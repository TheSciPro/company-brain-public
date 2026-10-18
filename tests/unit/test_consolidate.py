"""Consolidation policy: add-only when unattended, patches wait, small batches are skipped."""

from __future__ import annotations

from datetime import UTC, datetime

from deepagents.backends.protocol import (
    DeleteResult,
    EditResult,
    ReadResult,
    WriteResult,
)

from nwb.actions.models import ActionView
from nwb.agent.add_only import AddOnlyBackend
from nwb.conf import ConfigLoader, MemoryConfig
from nwb.memory.consolidate import apply_change, batch_decision, classify, prepare_transcript, wrong_store
from nwb.memory.guards import MemoryWriteGate, QualityGate, SecretGuard
from tests.conftest import ROOT

_SECRET = r"(?i)\b(password|passwd|pwd)\s*[:=]\s*\S+"


class _Files:
    def __init__(self, current: str = "before") -> None:
        self.current = current
        self.saved: tuple[str, str, str, str] | None = None

    def read(self, company_id: str, user_id: str, name: str) -> str | None:
        return self.current

    def write(self, company_id: str, user_id: str, name: str, text: str) -> None:
        self.saved = (company_id, user_id, name, text)
        self.current = text


def _gate() -> MemoryWriteGate:
    cfg = ConfigLoader(ROOT / "config").load("memory", MemoryConfig)
    return MemoryWriteGate(SecretGuard(cfg.secret_patterns), QualityGate(cfg.quality))


class _Memory:
    def __init__(self) -> None:
        self.calls = 0

    def append_fact(self, *_args: object, **_kwargs: object) -> None:
        self.calls += 1
        raise AssertionError("a removal must not write a fact")


class _Disk:
    """Just enough of a backend for the add-only gate."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.deleted: list[str] = []

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        if file_path not in self.files:
            return ReadResult(error="missing")
        return ReadResult(file_data={"content": self.files[file_path]})

    def write(self, file_path: str, content: str) -> WriteResult:
        self.files[file_path] = content
        return WriteResult(path=file_path)

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        text = self.files[file_path]
        if old_string not in text:
            return EditResult(error="missing")
        self.files[file_path] = (
            text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
        )
        return EditResult(path=file_path, occurrences=1)

    def delete(self, file_path: str) -> DeleteResult:
        self.deleted.append(file_path)
        self.files.pop(file_path, None)
        return DeleteResult(path=file_path)


def _action(op: str, path: str, after: str) -> ActionView:
    return ActionView(
        action_id="act-1",
        approval_id="apr-1",
        kind="memory_change",
        title=f"Memory {op}",
        status="approved",
        requester="NW1103",
        approver="NW1103",
        target="NW1103",
        args={"op": op, "path": path, "before": "before", "after": after, "asof": "2026-09-28"},
        proposed_by="consolidation",
    )


def test_batch_decision_skips_a_small_ended_batch_and_waits_while_open() -> None:
    assert batch_decision(2, 6, 3, ended=True) == "skip"
    assert batch_decision(2, 6, 3, ended=False) == "wait"
    assert batch_decision(4, 6, 3, ended=False) == "wait"
    assert batch_decision(4, 6, 3, ended=True) == "run"
    assert batch_decision(6, 6, 3, ended=False) == "run"
    assert batch_decision(3, 0, 3, ended=False) == "wait"
    assert batch_decision(3, 0, 3, ended=True) == "run"


def test_classify_add_patch_and_remove() -> None:
    before = "# Notes\n- (nothing yet: ask or observe, then write it here)\n"
    added = before.replace(
        "- (nothing yet: ask or observe, then write it here)",
        "<!-- observed: 2026-09-28 | status: active -->\n- Prefer short answers.",
    )
    assert classify(None, "I work on the Payments team.") == "add"
    assert classify(before, added) == "add"
    patched = added.replace("- Prefer short answers.", "- Prefer long answers.")
    assert classify(added, patched) == "patch"
    assert classify(added, before) == "remove"


def test_prepare_transcript_drops_a_password() -> None:
    body, rejected = prepare_transcript(
        [("user", "my password: hunter2secret"), ("user", "I like short answers"), ("assistant", "Noted.")],
        [SecretGuard([_SECRET])],
        400,
    )
    assert rejected == 1
    assert "hunter2" not in body
    assert "short answers" in body
    assert "Noted." in body


def test_one_fact_one_store() -> None:
    assert wrong_store("/memories/facts/note.md", None, "- Prefer short answers.")
    assert wrong_store("/memories/USER.md", None, "I work on the Payments team.")
    assert wrong_store("/memories/MEMORY.md", "# Index\n", "# Index\n- Prefer short answers.\n") is not None
    assert wrong_store("/memories/facts/note.md", None, "I work on the Payments team.") is None
    assert wrong_store("/memories/USER.md", None, "- Prefer short answers.") is None


def test_unattended_add_lands_and_patch_or_remove_does_not_change_the_file() -> None:
    disk = _Disk()
    disk.files["/memories/USER.md"] = "# Who\n- Name: Ada\n- (nothing yet: ask or observe, then write it here)\n"
    staged: list[tuple[str, str]] = []
    gate = AddOnlyBackend(disk, lambda op, path, _before, _after: staged.append((op, path)))  # type: ignore[arg-type]

    added = gate.edit(
        "/memories/USER.md",
        "- (nothing yet: ask or observe, then write it here)",
        "<!-- observed: 2026-09-28 | status: active -->\n- Prefer short answers.",
    )
    assert added.error is None
    assert gate.added == 1
    assert "Prefer short answers." in disk.files["/memories/USER.md"]

    held = gate.edit("/memories/USER.md", "- Prefer short answers.", "- Prefer long answers.")
    assert held.error and "waiting for approval" in held.error
    assert "Prefer short answers." in disk.files["/memories/USER.md"]
    assert "Prefer long answers." not in disk.files["/memories/USER.md"]
    assert staged == [("patch", "/memories/USER.md")]

    fresh = gate.write("/memories/facts/note.md", "I work on the Payments team.")
    assert fresh.error is None
    assert disk.files["/memories/facts/note.md"] == "I work on the Payments team."

    rejected = gate.write("/memories/facts/pref.md", "- Prefer short answers.")
    assert rejected.error and "USER.md" in rejected.error
    assert "/memories/facts/pref.md" not in disk.files

    removed = gate.delete("/memories/USER.md")
    assert removed.error and "nothing was deleted" in removed.error
    assert disk.deleted == []
    assert "/memories/USER.md" in disk.files
    assert staged[-1] == ("remove", "/memories/USER.md")
    assert gate.staged == 2


def test_approved_patch_is_written_and_an_approved_removal_is_not() -> None:
    files = _Files()
    memory = _Memory()
    note = apply_change(files, memory, _gate(), "co", _action("patch", "/memories/USER.md", "Prefer long answers."))  # type: ignore[arg-type]
    assert note.startswith("applied")
    assert files.saved == ("co", "NW1103", "USER.md", "Prefer long answers.")
    note = apply_change(files, memory, _gate(), "co", _action("remove", "/memories/USER.md", ""))  # type: ignore[arg-type]
    assert "not deleted" in note
    assert memory.calls == 0
    assert files.saved is not None and files.saved[3] == "Prefer long answers."


def test_a_stale_approved_patch_leaves_the_users_newer_edit_alone() -> None:
    files = _Files(current="before\n- Prefer tables for numbers.")  # the user edited after the proposal
    note = apply_change(files, _Memory(), _gate(), "co", _action("patch", "/memories/USER.md", "Prefer long answers."))  # type: ignore[arg-type]
    assert note.startswith("not applied") and "changed after this was proposed" in note
    assert files.saved is None and files.current == "before\n- Prefer tables for numbers."


def test_an_approved_patch_still_meets_the_profile_gate() -> None:
    files = _Files()
    trace = 'before\nTraceback (most recent call last):\n  File "w.py", line 4, in run\nRuntimeError: boom'
    note = apply_change(files, _Memory(), _gate(), "co", _action("patch", "/memories/USER.md", trace))  # type: ignore[arg-type]
    assert note.startswith("not applied") and files.saved is None


def test_fact_reference_time_is_timezone_aware() -> None:
    """append_fact must not be handed a naive datetime from a date-only asof."""

    class _Aware:
        def __init__(self) -> None:
            self.when: datetime | None = None

        def append_fact(self, _actor: object, content: str, _source: str, when: datetime) -> object:
            self.when = when
            assert content == "I work on the Payments team."
            return type("Receipt", (), {"episode_id": "ep", "rejected": [], "facts_written": 1})()

    memory = _Aware()
    note = apply_change(
        object(), memory, _gate(), "co", _action("patch", "/memories/facts/note.md", "I work on the Payments team.")
    )  # type: ignore[arg-type]
    assert "appended" in note
    assert memory.when is not None and memory.when.tzinfo is UTC
