"""Ingest-time fact extraction: LLM (background model) -> typed facts -> quote guard. Cached by document hash.

The extractor never decides authority, dates or scope from the client: those come from the source catalog and the
document's frontmatter. The model only says *what* the document asserts (key, value, unit, country qualifiers) and
*where* (a verbatim quote). Keys are discovered from the data: the prompt is given the keys extracted so far so the
same attribute gets the same key across versions and documents (mem0-style reuse of existing memories).

Modes (``Settings.extraction_mode``):
- ``llm``        call the model on a cache miss and store the result (``nwb extract`` records fixtures this way);
- ``cache_only`` replay recorded extractions (tests, CI, offline demos); a miss is logged and yields no facts;
- ``off``        no document facts (structured record facts still load).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nwb.conf.models import ExtractionConfig
from nwb.obs.log import get_logger
from nwb.prompts import PromptLibrary
from nwb.util.geo import normalize_country

log = get_logger(__name__)

ExtractionMode = Literal["llm", "cache_only", "off"]
_KEY = re.compile(r"[^a-z0-9]+")


class ExtractedFact(BaseModel):
    """One fact as the model reports it (validated; identity, authority and dates are added by ingest)."""

    model_config = ConfigDict(extra="ignore")
    key: str = Field(description="snake_case attribute name")
    label: str = Field(description="short human description of the attribute")
    value: str
    unit: str | None = None
    qualifiers: dict[str, str] = Field(default_factory=dict)
    quote: str = Field(min_length=3, max_length=400)

    @field_validator("key")
    @classmethod
    def _snake(cls, v: str) -> str:
        k = _KEY.sub("_", v.lower()).strip("_")
        if not k:
            raise ValueError("empty key")
        return k[:80]

    @field_validator("value", mode="before")
    @classmethod
    def _str(cls, v: object) -> str:
        return str(v).strip()

    @field_validator("qualifiers", mode="before")
    @classmethod
    def _quals(cls, v: object) -> dict[str, str]:
        if not isinstance(v, dict):
            return {}
        out: dict[str, str] = {}
        for k, val in v.items():
            if val in (None, "", [], {}):
                continue
            name = _KEY.sub("_", str(k).lower()).strip("_")
            out[name] = (normalize_country(str(val)) or "") if name == "country" else str(val)
        return {k: v for k, v in out.items() if v}


class Extraction(BaseModel):
    """What one document yielded (the cached/recorded unit)."""

    doc_id: str
    prompt_ref: str
    model: str | None = None
    facts: list[ExtractedFact] = Field(default_factory=list)


class _WireFact(BaseModel):
    """Strict JSON-schema shape the provider enforces (every field present; nullable instead of optional)."""

    model_config = ConfigDict(extra="forbid")
    key: str
    label: str
    value: str
    unit: str | None
    country: str | None = Field(description="ISO 3166-1 alpha-2 if the rule is limited to one country, else null")
    quote: str

    def to_fact(self) -> ExtractedFact | None:
        try:
            return ExtractedFact(
                key=self.key,
                label=self.label,
                value=self.value,
                unit=self.unit or None,
                qualifiers={"country": self.country} if self.country else {},
                quote=self.quote,
            )
        except ValueError:
            return None


class _ModelOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    facts: list[_WireFact]

    def to_facts(self) -> list[ExtractedFact]:
        return [f for w in self.facts if (f := w.to_fact()) is not None]


@dataclass(frozen=True, slots=True)
class KnownKey:
    key: str
    label: str
    unit: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentInput:
    doc_id: str
    title: str
    doc_type: str | None
    status: str | None
    client: str | None
    raw: str  # full file text (hash input)
    body: str  # text after frontmatter (what the model reads)


class FactExtractor(Protocol):
    def extract(self, doc: DocumentInput, known: Sequence[KnownKey]) -> Extraction | None: ...


class ExtractionCache:
    """``<dir>/<sha>.json``: one file per document *content*. Renaming or editing a document is a new key."""

    def __init__(self, root: Path) -> None:
        self._root = root

    @staticmethod
    def key(raw_text: str) -> str:
        return hashlib.sha256(raw_text.encode()).hexdigest()[:20]

    def get(self, raw_text: str) -> Extraction | None:
        p = self._root / f"{self.key(raw_text)}.json"
        if not p.is_file():
            return None
        return Extraction.model_validate_json(p.read_text())

    def put(self, raw_text: str, ex: Extraction) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        p = self._root / f"{self.key(raw_text)}.json"
        p.write_text(json.dumps(ex.model_dump(mode="json"), indent=1, ensure_ascii=False) + "\n")


Invoke = Callable[[str], _ModelOutput]


class LLMFactExtractor:
    """Structured-output call on the background model, with backoff for free-tier rate limits."""

    def __init__(self, invoke: Invoke, prompts: PromptLibrary, cfg: ExtractionConfig, model_name: str) -> None:
        self._invoke = invoke
        self._prompts = prompts
        self._cfg = cfg
        self._model = model_name

    @property
    def prompt_ref(self) -> str:
        return self._prompts.get(self._cfg.prompt).ref

    def extract(self, doc: DocumentInput, known: Sequence[KnownKey]) -> Extraction | None:
        rendered = self._prompts.render(
            self._cfg.prompt,
            title=doc.title,
            doc_type=doc.doc_type,
            status=doc.status,
            client=doc.client,
            body=doc.body[: self._cfg.max_chars],
            known_keys=list(known),
            max_facts=self._cfg.max_facts_per_doc,
        )
        last: Exception | None = None
        for attempt in range(self._cfg.retries + 1):
            try:
                out = self._invoke(rendered.text)
                return Extraction(
                    doc_id=doc.doc_id,
                    prompt_ref=rendered.ref,
                    model=self._model,
                    facts=out.to_facts()[: self._cfg.max_facts_per_doc],
                )
            except Exception as exc:  # provider errors vary (429, schema, timeouts): retry, then give up loudly
                last = exc
                log.warning("extraction %s attempt %d failed: %s", doc.doc_id, attempt + 1, str(exc)[:200])
                time.sleep(self._cfg.retry_wait_s * (attempt + 1))
        log.error("extraction gave up on %s: %s", doc.doc_id, last)
        return None


class CachedExtractor:
    """Cache in front of an optional live extractor (``None`` = replay only)."""

    def __init__(self, cache: ExtractionCache, live: FactExtractor | None) -> None:
        self._cache = cache
        self._live = live
        self.misses: list[str] = []

    def extract(self, doc: DocumentInput, known: Sequence[KnownKey]) -> Extraction | None:
        hit = self._cache.get(doc.raw)
        current = getattr(self._live, "prompt_ref", None)
        if hit is not None and (current is None or hit.prompt_ref == current):
            return hit  # replay mode accepts any recorded prompt; live mode re-extracts when the prompt changed
        if self._live is None:
            self.misses.append(doc.doc_id)
            log.warning("no recorded extraction for %s (cache_only); run `nwb extract` to record it", doc.doc_id)
            return None
        ex = self._live.extract(doc, known)
        if ex is not None:
            self._cache.put(doc.raw, ex)
        return ex


def structured_invoke(model: object) -> Invoke:
    """Adapter: a LangChain chat model -> ``prompt -> _ModelOutput`` (JSON-schema structured output)."""
    from langchain_core.language_models import BaseChatModel

    assert isinstance(model, BaseChatModel)
    runnable = model.with_structured_output(_ModelOutput, method="json_schema", strict=True)

    def call(prompt: str) -> _ModelOutput:
        out = runnable.invoke(prompt)
        return out if isinstance(out, _ModelOutput) else _ModelOutput.model_validate(out)

    return call
