"""Learn a preference from a human edit of a drafted action (Sylph), stored as a USER.md directive.

The diff is deterministic: each argument the approver changed becomes one scope (``<kind>.<field>``).
The configured model only phrases the changed words of that field (see ``prompts/directive_phrase.md``),
not a prefix of the whole value. A phrase that drops an added word is discarded for a quote of the
new wording. Only that new directive block is screened by ``MemoryWriteGate`` — the rest of USER.md
is already stored. Each field returns its own receipt: a stored line, or a refusal with the reason
and never the line. ``DirectiveRejectedError`` is raised only when every changed field was refused.
A conflicting active directive with the same scope is marked superseded in place.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from nwb.agent.context import RuntimeContext
from nwb.agent.memory_files import MemoryFiles
from nwb.conf.models import EditLearningConfig
from nwb.memory.directives import Directive, apply_directive, format_directive
from nwb.memory.guards import MemoryWriteGate
from nwb.memory.phrasing import DirectivePhraser, PhraseRequest, RecordingMissError, keeps_concrete_change
from nwb.obs.log import Hop, dataflow, get_logger

log = get_logger(__name__)
_ELLIPSIS = " … "


class DirectiveRejectedError(Exception):
    """Every changed field was refused by the memory write gate. Nothing was stored.

    ``str(self)`` joins the per-field reasons. It never includes the refused text.
    ``receipts`` is one entry per refused field.
    """

    def __init__(self, reason: str, receipts: Sequence[Mapping[str, str]] | None = None) -> None:
        super().__init__(reason)
        self.receipts: list[dict[str, str]] = [dict(item) for item in receipts] if receipts else []


@dataclass(frozen=True, slots=True)
class FieldChange:
    field: str
    before: str
    after: str


def norm_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.split())
    return str(value)


def diff_args(before: Mapping[str, object], after: Mapping[str, object], ignore: frozenset[str]) -> list[FieldChange]:
    """Fields the approver set to a different value. Missing keys in ``after`` are left as drafted."""
    changes: list[FieldChange] = []
    for key, new in after.items():
        if key in ignore or not isinstance(key, str):
            continue
        old = before.get(key)
        old_s, new_s = norm_value(old), norm_value(new)
        if old_s != new_s:
            changes.append(FieldChange(field=key, before=old_s, after=new_s))
    return changes


def changed_spans(before: str, after: str) -> tuple[str, str]:
    """Words that differ, in order. Shared wording is left out so a long prefix cannot hide the edit.

    Compared as words, not characters: a character diff splits ``Monday`` into fragments the
    concrete-word check would then fail to find in a normal sentence.
    """
    old_words, new_words = before.split(), after.split()
    matcher = difflib.SequenceMatcher(a=old_words, b=new_words, autojunk=False)
    removed: list[str] = []
    added: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag in {"delete", "replace"}:
            removed.extend(old_words[i1:i2])
        if tag in {"insert", "replace"}:
            added.extend(new_words[j1:j2])
    return " ".join(removed), " ".join(added)


def bound_span(text: str, limit: int) -> str:
    """Keep both ends of a long span. A prefix slice would drop a change that sits at the end."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= len(_ELLIPSIS):
        return text[-limit:]
    room = limit - len(_ELLIPSIS)
    head, tail = room // 2, room - (room // 2)
    return f"{text[:head].rstrip()}{_ELLIPSIS}{text[-tail:].lstrip()}"


def fallback_phrase(kind: str, change: FieldChange, max_chars: int) -> str:
    """A directive sentence built only from the diff, used when the model returns nothing usable.

    Quotes the changed wording, using the whole ``max_chars`` budget. The end of that wording is
    kept when it does not fit: the edit is often a suffix, and a prefix slice would drop it.
    """
    _removed, added = changed_spans(change.before, change.after)
    quoted = added if added else change.after
    prefix = f'For {kind or "this action"}, {change.field} should be "'
    suffix = '".'
    room = max_chars - len(prefix) - len(suffix)
    if room < 1:
        return prefix[:max_chars].rstrip()
    if len(quoted) > room:
        quoted = bound_span(quoted, room)
    return f"{prefix}{quoted}{suffix}"


def _scope(kind: str, field: str) -> str:
    raw = f"{kind}.{field}" if kind else field
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in raw) or "edit"


class EditLearning:
    def __init__(
        self,
        files: MemoryFiles,
        phraser: DirectivePhraser,
        cfg: EditLearningConfig,
        gate: MemoryWriteGate,
    ) -> None:
        self._files = files
        self._phraser = phraser
        self._cfg = cfg
        self._gate = gate

    def learn(
        self,
        ctx: RuntimeContext,
        drafts: Sequence[Mapping[str, object]],
        decisions: Sequence[Mapping[str, object]],
    ) -> list[dict[str, str]]:
        """Write one directive per changed field.

        Returns one receipt per field that was stored or refused. Raises ``DirectiveRejectedError``
        only when every changed field was refused, so a stored line is not reported as a pure rejection.
        """
        if not self._cfg.enabled:
            return []
        receipts: list[dict[str, str]] = []
        for draft, decision in zip(drafts, decisions, strict=False):
            if decision.get("type") != "edit":
                continue
            edited = decision.get("edited_action")
            if not isinstance(edited, Mapping):
                continue
            tool = str(draft.get("name") or "")
            edited_name = edited.get("name")
            if isinstance(edited_name, str) and edited_name and edited_name != tool:
                log.info("edit renamed the tool; no directive", before=tool, after=edited_name)
                continue
            before = draft.get("args") if isinstance(draft.get("args"), Mapping) else {}
            after = edited.get("args") if isinstance(edited.get("args"), Mapping) else {}
            kind = str(after.get(self._cfg.kind_field) or before.get(self._cfg.kind_field) or tool)
            for change in diff_args(before, after, frozenset(self._cfg.ignore_fields)):
                receipt = self._write_one(ctx, tool, kind, change)
                if receipt is not None:
                    receipts.append(receipt)
        refused = [item for item in receipts if item["event"] == "REJECTED"]
        if refused and len(refused) == len(receipts):
            raise DirectiveRejectedError(" ".join(item["reason"] for item in refused), refused)
        return receipts

    def _write_one(self, ctx: RuntimeContext, tool: str, kind: str, change: FieldChange) -> dict[str, str] | None:
        phrase = self._phrase(tool, kind, change)
        scope = _scope(kind, change.field)
        directive = Directive(
            verb=self._cfg.verb,
            text=phrase,
            observed=ctx.asof,
            status=self._cfg.active_status,
            scope=scope,
        )
        # Gate the new block only. Scanning the whole profile lets a seeded file (already past
        # the error-dump length) reject a clean line because of words elsewhere in working context.
        line = format_directive(directive)
        name = self._cfg.target_file
        if reason := self._gate.reject_text(line):
            _log_rejection(name, reason)
            return {"event": "REJECTED", "scope": scope, "path": name, "reason": reason}
        current = self._files.read(ctx.company_id, ctx.user_id, name) or ""
        updated = apply_directive(current, directive, self._cfg)
        if updated == current:
            return None
        self._files.write(ctx.company_id, ctx.user_id, name, updated)
        log.info("directive learned from edit", person=ctx.user_id, scope=scope, verb=self._cfg.verb)
        dataflow(
            Hop.MEMORY_WRITE,
            event="DIRECTIVE",
            person=ctx.user_id,
            path=name,
            scope=scope,
            verb=self._cfg.verb,
        )
        return {"event": "DIRECTIVE", "scope": scope, "path": name}

    def _phrase(self, tool: str, kind: str, change: FieldChange) -> str:
        before_span, after_span = changed_spans(change.before, change.after)
        limit = self._cfg.excerpt_chars
        request = PhraseRequest(
            tool=tool,
            kind=kind,
            field=change.field,
            before=bound_span(before_span, limit),
            after=bound_span(after_span, limit),
        )
        text: str | None = None
        try:
            text = self._phraser.phrase(request)
        except RecordingMissError:
            raise
        except Exception:
            log.exception("directive phrasing failed; using the diff")
        # The model saw a bounded span. The keep-check uses the full spans, so a word past the
        # bound still has to appear; otherwise a vague sentence would be stored.
        if text and not keeps_concrete_change(before_span, after_span, text):
            log.info("directive phrase omitted a concrete change; discarding it", field=change.field)
            text = None
        return text or fallback_phrase(kind, change, self._cfg.max_chars)


def _log_rejection(path: str, reason: str) -> None:
    log.info("learned directive rejected", path=path, reason=reason)
    dataflow(Hop.MEMORY_WRITE, event="REJECTED", path=path, reason=reason)
