"""Decisions for the unattended consolidation pass.

The pass may only add. A patch or a removal is classified here and left for an approval; nothing in this
module deletes. A batch smaller than the configured minimum is skipped, which means the files are not
rewritten. Preferences and directives belong in USER.md. Dated facts belong in Graphiti (a new file under
``/memories/facts/``). One fact, one store.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal

from nwb.actions.models import ActionView
from nwb.agent.memory_files import FILES, MemoryFiles
from nwb.identity import Actor
from nwb.memory.guards import MemoryWriteGate, WriteGuard
from nwb.memory.store import PersonalMemory, rejection, screen_user_text

Change = Literal["add", "patch", "remove"]
Decision = Literal["wait", "skip", "run"]

_META = re.compile(r"^<!-- observed: \d{4}-\d{2}-\d{2} \| status: (?:active|superseded) -->$")
_ITEM = re.compile(r"^- (?:Always|Never|Prefer) \S")
_INDEX = re.compile(r"^- \d{4}-\d{2}-\d{2} /memories/facts/\S+$")
_DATED = re.compile(r"(?i)\b(team|office|moved|moving|works on|working on|client|project)\b")
_PLACEHOLDER = "- (nothing yet"


def batch_decision(pending: int, interval: int, minimum: int, *, ended: bool) -> Decision:
    """Whether this batch should start the agent.

    ``pending`` is the number of new user messages since the last successful pass. Below ``minimum``
    the batch is too small: skip it when the session has ended, otherwise keep waiting. The turn
    counter (``interval``) starts a pass on an open session. ``interval`` of 0 disables that counter.
    Ending a session still runs a batch that is large enough.
    """
    if pending < minimum:
        return "skip" if ended else "wait"
    if ended or (interval > 0 and pending >= interval):
        return "run"
    return "wait"


def classify(before: str | None, after: str) -> Change:
    """Add when nothing meaningful was removed. A changed line is a patch. A dropped line is a removal."""
    if before is None:
        return "add"
    removed, added = _delta(before, after)
    if removed and added:
        return "patch"
    if removed:
        return "remove"
    return "add"


def added_are_directives(before: str, after: str) -> bool:
    """New USER.md lines are directive metadata or an Always/Never/Prefer line."""
    for line in _added(before, after):
        if line.startswith("#") or line.startswith(_PLACEHOLDER):
            continue
        if not (_META.match(line) or _ITEM.match(line)):
            return False
    return True


def index_lines_only(before: str, after: str) -> bool:
    """MEMORY.md may gain a date plus a fact path. That is a pointer, not a second copy of the fact."""
    for line in _added(before, after):
        if line.startswith("#"):
            continue
        if not _INDEX.match(line):
            return False
    return True


def wrong_store(path: str, before: str | None, after: str) -> str | None:
    """Reject a write that would put one fact in two stores. ``None`` means the path is acceptable."""
    lines = _added(before, after) if before is not None else [ln.strip() for ln in after.splitlines() if ln.strip()]
    text = "\n".join(lines)
    if "/facts/" in path and _looks_like_directive(text):
        return "preferences and directives go in /memories/USER.md only"
    if path.endswith("USER.md") and _looks_like_dated_fact(text) and not _looks_like_directive(text):
        return "dated facts go under /memories/facts/ only"
    if path.endswith("MEMORY.md") and (_looks_like_directive(text) or _looks_like_dated_fact(text)):
        return "MEMORY.md is an index of fact paths, not a second copy of the fact"
    return None


def prepare_transcript(
    messages: Sequence[tuple[str, str]], guards: Sequence[WriteGuard], chunk_chars: int
) -> tuple[str, int]:
    """User text that passes the write guards, plus assistant lines that are not secrets.

    Returns the prompt body and how many user chunks were rejected. Rejected text is not included.
    """
    lines: list[str] = []
    rejected = 0
    for role, content in messages:
        if role == "user":
            kept, reasons = screen_user_text(guards, [("user", content)], chunk_chars)
            rejected += len(reasons)
            if kept:
                lines.append(f"user: {kept}")
            continue
        if not content.strip():
            continue
        if rejection(guards, content[:chunk_chars], content):
            continue
        lines.append(f"assistant: {content[:chunk_chars]}")
    return "\n".join(lines), rejected


def apply_change(
    files: MemoryFiles, memory: PersonalMemory, gate: MemoryWriteGate, company_id: str, action: ActionView
) -> str:
    """Apply an approved memory patch. A removal is not applied: nothing is deleted.

    A file patch is written only if the file still reads exactly as it did when the change was proposed, and
    only if the lines it adds pass the profile gate (the same gate a live edit meets). Otherwise nothing changes.
    """
    op = str(action.args.get("op") or "")
    path = str(action.args.get("path") or "")
    after = str(action.args.get("after") or "")
    if op == "remove":
        return "not deleted; the file and the fact were left unchanged"
    if "/facts/" in path:
        if not after.strip():
            return "not deleted; the dated fact stays"
        asof = str(action.args.get("asof") or "")
        when = datetime.fromisoformat(asof).replace(tzinfo=UTC) if asof else datetime.now(UTC)
        actor = Actor(company_id, action.requester, when.date())
        receipt = memory.append_fact(actor, after, after, when)
        if receipt.rejected or not receipt.episode_id:
            reason = receipt.rejected[0] if receipt.rejected else "fact was not stored"
            return f"not applied: {reason}"
        return f"appended {receipt.facts_written}; nothing was deleted"
    name = path.rsplit("/", 1)[-1]
    if name not in FILES:
        return "not applied: path is outside memory files"
    before = action.args.get("before")
    current = files.read(company_id, action.requester, name)
    if before is None or current != str(before):
        return f"not applied: {name} changed after this was proposed; the file was left as it is"
    if reason := gate.reject(path, "\n".join(_added(current, after))):
        return f"not applied: {reason}"
    files.write(company_id, action.requester, name, after)
    return f"applied {op} to {name}"


def _meaningful(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(_PLACEHOLDER):
            continue
        if line.startswith("<!--") and "Directives" in line:
            continue
        lines.append(line)
    return lines


def _added(before: str | None, after: str) -> list[str]:
    old: Counter[str] = Counter(_meaningful(before or ""))
    new: Counter[str] = Counter(_meaningful(after))
    return list((new - old).elements())


def _delta(before: str, after: str) -> tuple[list[str], list[str]]:
    old: Counter[str] = Counter(_meaningful(before))
    new: Counter[str] = Counter(_meaningful(after))
    return list((old - new).elements()), list((new - old).elements())


def _looks_like_directive(text: str) -> bool:
    for raw in text.splitlines():
        line = raw.strip()
        if _META.match(line) or _ITEM.match(line):
            return True
        if re.match(r"(?i)^(always|never|prefer)\b", line):
            return True
    return False


def _looks_like_dated_fact(text: str) -> bool:
    return bool(text.strip()) and _DATED.search(text) is not None
