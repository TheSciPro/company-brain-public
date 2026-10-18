"""JobRepository: ``jobs`` definitions + idempotent ``job_runs`` (idempotency_key is UNIQUE)."""

from __future__ import annotations

from datetime import UTC, datetime

from nwb.db.repo_sessions import new_id
from nwb.ports import UnitOfWork


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class JobRepository:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    def ensure_job(self, name: str, kind: str, schedule: str | None = None) -> str:
        r = self._uow.fetchone("SELECT id FROM jobs WHERE name = :n", {"n": name})
        if r:
            return str(r["id"])
        jid = f"job-{name}"
        self._uow.execute(
            "INSERT INTO jobs (id, name, kind, schedule) VALUES (:id, :n, :k, :s)",
            {"id": jid, "n": name, "k": kind, "s": schedule},
        )
        return jid

    def claim(self, job_id: str, key: str, scope_type: str | None = None, scope_id: str | None = None) -> str | None:
        """Start a run unless one with this idempotency key already finished. Returns the run id or None."""
        r = self._uow.fetchone("SELECT id, status FROM job_runs WHERE idempotency_key = :k", {"k": key})
        if r and r["status"] in {"done", "skipped", "running"}:
            return None
        if r:
            self._uow.execute(
                "UPDATE job_runs SET status = 'running', attempts = attempts + 1, started_at = :t, error = NULL "
                "WHERE id = :id",
                {"id": r["id"], "t": _now()},
            )
            return str(r["id"])
        rid = new_id("jr")
        self._uow.execute(
            "INSERT INTO job_runs (id, job_id, scope_type, scope_id, status, attempts, started_at, idempotency_key) "
            "VALUES (:id, :j, :st, :si, 'running', 1, :t, :k)",
            {"id": rid, "j": job_id, "st": scope_type, "si": scope_id, "t": _now(), "k": key},
        )
        return rid

    def finish(
        self, run_id: str, status: str, *, items_in: int = 0, items_out: int = 0, error: str | None = None
    ) -> None:
        self._uow.execute(
            "UPDATE job_runs SET status = :s, is_done = :d, finished_at = :t, items_in = :i, items_out = :o, "
            "error = :e WHERE id = :id",
            {"id": run_id, "s": status, "d": status == "done", "t": _now(), "i": items_in, "o": items_out, "e": error},
        )
