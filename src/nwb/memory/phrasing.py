"""Phrase one directive from a deterministic edit diff. The model only writes the sentence.

The sentence must keep the concrete change: every word the approver added or replaced
(a deadline, a count, a name, the new wording). A vaguer sentence that drops those words
is discarded, and the caller quotes the diff instead. Generalising here would store a
preference the person did not approve.

The diff (which field changed, from what, to what) is computed before this module runs. A recorded
call is a JSON file whose ``request`` matches that diff; replay returns it and does not call the model.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage

from nwb.conf.models import EditLearningConfig
from nwb.obs.log import Hop, dataflow, get_logger
from nwb.prompts import PromptLibrary

log = get_logger(__name__)

RecordMode = Literal["live", "record", "replay"]
# Glue words. A number, a name, or a deadline is never in this set.
_GLUE = frozenset({"a", "an", "the", "to", "of", "in", "on", "for", "and", "or", "with", "by", "it", "is", "be"})
_WORD = re.compile(r"[A-Za-z0-9]+")


class RecordingMissError(LookupError):
    """Replay mode had no stored call for this diff."""


def canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class PhraseRequest:
    """The diff the model is allowed to see. Nothing here is invented by the model."""

    tool: str
    kind: str
    field: str
    before: str
    after: str

    def payload(self) -> dict[str, str]:
        return {
            "tool": self.tool,
            "kind": self.kind,
            "field": self.field,
            "before": self.before,
            "after": self.after,
        }


class PhraseRecorder:
    """``<dir>/*.json`` files: ``{"request": {...}, "response": "<model text>"}``."""

    def __init__(self, directory: Path, mode: RecordMode = "live") -> None:
        self._dir = directory
        self._mode = mode

    @property
    def mode(self) -> RecordMode:
        return self._mode

    def lookup(self, payload: Mapping[str, Any]) -> str | None:
        if self._mode == "live" or not self._dir.is_dir():
            return None
        want = canonical(payload)
        for path in sorted(self._dir.glob("*.json")):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                log.warning("skipping unreadable phrase recording", path=str(path))
                continue
            request = doc.get("request")
            if isinstance(request, dict) and canonical(request) == want:
                response = doc.get("response")
                return response if isinstance(response, str) else None
        return None

    def store(self, payload: Mapping[str, Any], response: str) -> None:
        if self._mode != "record":
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256(canonical(payload).encode()).hexdigest()[:16] + ".json"
        doc = {"request": dict(payload), "response": response}
        (self._dir / name).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


class DirectivePhraser(Protocol):
    def phrase(self, request: PhraseRequest) -> str | None: ...


def _words(text: str) -> set[str]:
    return {m.group(0).lower() for m in _WORD.finditer(text)}


def keeps_concrete_change(before: str, after: str, phrase: str) -> bool:
    """True when ``phrase`` still contains every content word the edit added or replaced.

    Choice: keep the concrete change. A directive that generalises it (dropping a deadline
    the approver typed) is not the preference they approved.
    """
    added = (_words(after) - _words(before)) - _GLUE
    if not added:
        return True
    return added <= _words(phrase)


def clean_phrase(raw: str, *, max_chars: int, verbs: tuple[str, ...]) -> str:
    """Take the model's ``{"text": "..."}`` (or a bare sentence) and return one line, or ''."""
    text = raw.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return ""
        if not isinstance(data, dict):
            return ""
        value = data.get("text")
        text = value.strip() if isinstance(value, str) else ""
    for verb in verbs:
        prefix = verb.lower()
        lowered = text.lower()
        if lowered.startswith(prefix + ":"):
            text = text.split(":", 1)[1].strip()
        elif lowered.startswith(prefix + " "):
            text = text[len(verb) :].strip(" :")
    text = " ".join(text.replace("-->", "").split())
    if len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return text


class LLMPhraser:
    """Calls the configured model unless a recording matches the diff."""

    def __init__(
        self,
        model: BaseChatModel,
        prompts: PromptLibrary,
        cfg: EditLearningConfig,
        recorder: PhraseRecorder | None = None,
    ) -> None:
        self._model = model
        self._prompts = prompts
        self._cfg = cfg
        self._recorder = recorder

    def phrase(self, request: PhraseRequest) -> str | None:
        payload = request.payload()
        if self._recorder is not None:
            hit = self._recorder.lookup(payload)
            if hit is not None:
                dataflow(Hop.LLM_CALL, role=self._cfg.role, kind="directive_phrase", source="replay")
                return self._usable(request, hit)
            if self._recorder.mode == "replay":
                raise RecordingMissError(f"no recorded directive phrase for {canonical(payload)}")
        rendered = self._prompts.render(
            self._cfg.prompt,
            tool=request.tool,
            kind=request.kind,
            field=request.field,
            before=request.before,
            after=request.after,
            max_chars=self._cfg.max_chars,
        )
        message = self._model.invoke([HumanMessage(content=rendered.text)])
        content = message.content
        raw = content if isinstance(content, str) else str(content)
        if self._recorder is not None:
            self._recorder.store(payload, raw)
        dataflow(Hop.LLM_CALL, role=self._cfg.role, kind="directive_phrase", source="model", prompt=rendered.ref)
        return self._usable(request, raw)

    def _usable(self, request: PhraseRequest, raw: str) -> str | None:
        phrase = clean_phrase(raw, max_chars=self._cfg.max_chars, verbs=self._cfg.verbs)
        if phrase and not keeps_concrete_change(request.before, request.after, phrase):
            log.info("directive phrase omitted a concrete change; discarding it", field=request.field)
            return None
        return phrase or None
