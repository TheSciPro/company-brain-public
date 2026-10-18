"""Question -> fact keys. The keys are whatever extraction discovered in the data; nothing is listed by hand.

Each key is indexed by its words, its labels, the titles of the documents that state it and their quotes. A query
is scored lexically (content-word overlap, light stemming) and by embedding similarity of the key's label, then
blended (``knowledge.yaml: resolve.lexical_weight``). The tool returns the top few, so the model can pick.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from nwb.conf.models import ResolveConfig
from nwb.memory.embeddings import Embedder, cosine
from nwb.ports import Row

_WORD = re.compile(r"[a-z0-9]+")


def stem(word: str) -> str:
    """Tiny suffix stripper: enough to match 'days'/'day', 'approvals'/'approval'. Not language-specific beyond -s."""
    for suf in ("ies", "es", "s"):
        if len(word) > 4 and word.endswith(suf):
            return word[: -len(suf)] + ("y" if suf == "ies" else "")
    return word


def terms(text: str, stopwords: frozenset[str]) -> set[str]:
    return {stem(w) for w in _WORD.findall(text.lower()) if len(w) > 1 and w not in stopwords}


@dataclass
class KeyEntry:
    key: str
    label: str | None = None
    unit: str | None = None
    words: set[str] = field(default_factory=set)  # key + labels + titles (strong evidence)
    context: set[str] = field(default_factory=set)  # quotes (weak evidence)
    vector: list[float] | None = None


@dataclass(frozen=True, slots=True)
class KeyMatch:
    key: str
    label: str | None
    unit: str | None
    score: float


class FactKeyIndex:
    def __init__(self, rows: Sequence[Row], embedder: Embedder, cfg: ResolveConfig, stopwords: frozenset[str]) -> None:
        self._cfg = cfg
        self._stop = stopwords
        self._embedder = embedder
        entries: dict[str, KeyEntry] = {}
        labels: dict[str, Counter[str]] = {}
        for r in rows:
            e = entries.setdefault(str(r["key"]), KeyEntry(str(r["key"])))
            if r["label"]:
                labels.setdefault(e.key, Counter())[str(r["label"])] += 1
            e.unit = e.unit or (str(r["unit"]) if r["unit"] else None)
            e.words |= terms(" ".join(filter(None, [e.key.replace("_", " "), r["label"], r["title"]])), stopwords)
            e.context |= terms(str(r["quote"] or ""), stopwords)
        for key, c in labels.items():  # the label most sources agree on (ties: the shorter one) represents the key
            entries[key].label = min(c, key=lambda lb: (-c[lb], len(lb), lb))
        self._entries = list(entries.values())
        if self._entries:
            vecs = embedder.embed([self._text(e) for e in self._entries])
            for e, v in zip(self._entries, vecs, strict=True):
                e.vector = v

    @staticmethod
    def _text(e: KeyEntry) -> str:
        return f"{e.label or ''} ({e.key.replace('_', ' ')})"

    def __len__(self) -> int:
        return len(self._entries)

    def resolve(self, question: str, k: int | None = None) -> list[KeyMatch]:
        q = terms(question, self._stop)
        if not q or not self._entries:
            return []
        qv = self._embedder.embed([question])[0]
        w = self._cfg.lexical_weight
        scored: list[KeyMatch] = []
        for e in self._entries:
            strong = len(q & e.words) / len(q)
            weak = len(q & (e.context - e.words)) / len(q)
            lexical = min(1.0, strong + 0.5 * weak)
            sem = max(0.0, cosine(qv, e.vector)) if e.vector is not None else 0.0
            score = w * lexical + (1 - w) * sem
            if score >= self._cfg.min_score:
                scored.append(KeyMatch(e.key, e.label, e.unit, round(score, 4)))
        scored.sort(key=lambda m: (-m.score, m.key))
        return scored[: k or self._cfg.top_k]
