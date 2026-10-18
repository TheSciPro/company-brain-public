"""graph_sync: drain graph_outbox in id order into FalkorDB. On failure: attempts++, keep error, stop."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from nwb.db.outbox import OutboxEntry, OutboxRepository
from nwb.graph.projection import DEFAULT_PROJECTORS, Projector
from nwb.obs.log import Hop, dataflow, get_logger, traced
from nwb.ports import GraphStore, SqlDatabase

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SyncReport:
    processed: int
    failed: int
    pending: int


class GraphSync:
    def __init__(
        self,
        db: SqlDatabase,
        graph: GraphStore,
        projectors: Iterable[Projector] = DEFAULT_PROJECTORS,
        batch_size: int = 500,
    ) -> None:
        self._db = db
        self._graph = graph
        self._projectors = {p.aggregate: p for p in projectors}
        self._batch = batch_size

    def run_once(self) -> SyncReport:
        with self._db.unit_of_work() as uow:
            entries = OutboxRepository(uow).pending(self._batch)
        dataflow(Hop.OUTBOX, pending_batch=len(entries))
        processed = failed = 0
        for run in _runs(entries):
            ids = [e.id for e in run]
            try:
                projector = self._projectors.get(run[0].aggregate)
                if projector is None:
                    raise LookupError(f"no projector for aggregate {run[0].aggregate!r}")
                projector.apply(self._graph, [e.payload for e in run])
            except Exception as exc:
                log.exception("graph_sync failed on outbox ids %s..%s", ids[0], ids[-1])
                with self._db.unit_of_work() as uow:
                    OutboxRepository(uow).mark_failed(ids, f"{type(exc).__name__}: {exc}")
                failed += len(ids)
                break  # keep id order: later entries may depend on this run
            with self._db.unit_of_work() as uow:
                OutboxRepository(uow).mark_processed(ids)
            dataflow(Hop.GRAPH_SYNC, aggregate=run[0].aggregate, op=run[0].op, first=ids[0], last=ids[-1], n=len(ids))
            processed += len(ids)
        with self._db.unit_of_work() as uow:
            pending = OutboxRepository(uow).count_pending()
        return SyncReport(processed, failed, pending)

    @traced(step="graph_sync.drain", level=logging.INFO)
    def drain(self, max_batches: int = 10_000) -> SyncReport:
        total = 0
        for _ in range(max_batches):
            rep = self.run_once()
            total += rep.processed
            if rep.failed or rep.pending == 0 or rep.processed == 0:
                return SyncReport(total, rep.failed, rep.pending)
        return SyncReport(total, 0, -1)


def _runs(entries: list[OutboxEntry]) -> list[list[OutboxEntry]]:
    """Group consecutive entries with the same (aggregate, op) so each run is one UNWIND batch."""
    runs: list[list[OutboxEntry]] = []
    for e in entries:
        if runs and (runs[-1][0].aggregate, runs[-1][0].op) == (e.aggregate, e.op):
            runs[-1].append(e)
        else:
            runs.append([e])
    return runs
