"""Copy the SQL memory tables into each owner's Graphiti group before migration 0018 drops them.

Every active ``memories`` row becomes one fact episode, every ``session_notes`` row one episode, both in the
group ``{company}__{user}`` of the person who owns the row, dated by the row's own timestamps. Writes go through
``PersonalMemory`` with the same write guards as a new memory. A guard rejection or a timeout leaves the row
uncopied, so 0018 refuses to drop. Hidden memories were removed from recall by their owner and are left behind
on purpose; they count as handled. ``memory_versions`` and ``memory_embeddings`` hang off ``memories``: older
versions were replaced by the current content and Graphiti embeds what it stores.

Graphiti episodes are not idempotent, so progress is kept per row (``RowLedger``) and a rerun resumes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, date, datetime

from nwb.db.copy_before_drop import RowLedger
from nwb.errors import MigrationError
from nwb.identity import Actor
from nwb.memory.store import EpisodeReceipt, PersonalMemory
from nwb.obs.log import get_logger
from nwb.ports import Row, SqlDatabase

log = get_logger(__name__)


class _ToGraphiti(ABC):
    """Shared loop: read rows, write each through ``PersonalMemory``, mark it, refuse on the first failure."""

    table: str
    _select: str

    def __init__(self, memory: Callable[[], PersonalMemory]) -> None:
        self._memory = memory

    def copy(self, db: SqlDatabase) -> int:
        with db.unit_of_work() as uow:
            rows = uow.fetchall(self._select)
        ledger = RowLedger(db, self.table)
        done = ledger.done()
        handled = 0
        memory: PersonalMemory | None = None
        failed: list[str] = []
        for row in rows:
            row_id = str(row["id"])
            if row_id in done or not self._carried(row):
                handled += 1
                continue
            memory = memory or self._memory()
            receipt = self._write(memory, row, _actor(row))
            if receipt.episode_id is None or receipt.rejected:
                failed.append(f"{row_id} ({'; '.join(receipt.rejected) or 'not written'})")
                continue
            ledger.mark(row_id)
            handled += 1
        if failed:
            log.warning("rows not copied before drop", table=self.table, failed=len(failed))
            raise MigrationError(f"{len(failed)} {self.table} rows did not reach Graphiti: {', '.join(failed[:5])}")
        return handled

    def _carried(self, row: Row) -> bool:
        del row
        return True

    @abstractmethod
    def _write(self, memory: PersonalMemory, row: Row, actor: Actor) -> EpisodeReceipt: ...


class SqlMemoriesToGraphiti(_ToGraphiti):
    table = "memories"
    _select = (
        "SELECT m.id, p.company_id, m.person_id, m.content, m.status, COALESCE(m.updated_at, m.created_at) AS at "
        "FROM memories m JOIN people p ON p.id = m.person_id ORDER BY m.id"
    )

    def _carried(self, row: Row) -> bool:
        return row["status"] == "active"

    def _write(self, memory: PersonalMemory, row: Row, actor: Actor) -> EpisodeReceipt:
        content = str(row["content"])
        return memory.append_fact(actor, content, content, _moment(row["at"]))


class SqlSessionNotesToGraphiti(_ToGraphiti):
    table = "session_notes"
    _select = (
        "SELECT n.id, p.company_id, n.person_id, n.session_id, n.summary, n.created_at AS at "
        "FROM session_notes n JOIN people p ON p.id = n.person_id ORDER BY n.id"
    )

    def _write(self, memory: PersonalMemory, row: Row, actor: Actor) -> EpisodeReceipt:
        return memory.remember_chat(actor, [("user", str(row["summary"]))], [], _moment(row["at"]))


def _actor(row: Row) -> Actor:
    when = _moment(row["at"])
    return Actor(str(row["company_id"]), str(row["person_id"]), when.date(), session_id=row.get("session_id"))


def _moment(value: object) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, date):
        stamp = datetime(value.year, value.month, value.day)
    else:
        stamp = datetime.fromisoformat(str(value))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
