"""A deterministic stand-in for GraphRAG-SDK's LLM interface.

Used when no provider key is configured. It never calls a network. Answers are shaped like the
SDK's discovery and extraction prompts so ``Ontology.from_sources`` and ``GraphExtraction`` can
finish offline. This is not a live model; reports must say so.
"""

from __future__ import annotations

import json
import re
from typing import Any

from graphrag_sdk.core.models import LLMResponse
from graphrag_sdk.core.providers.base import LLMInterface

_PROPER = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b")
_REL_TYPE = re.compile(r"\b([A-Z][A-Z0-9_]{2,})\b")
_ORG_TAIL = frozenset({"desk", "team", "group", "office", "inc", "ltd", "corp", "department"})

_ONTOLOGY = {
    "entities": [
        {
            "label": "Person",
            "description": "A named person.",
            "properties": [{"name": "name", "type": "STRING", "description": "Display name."}],
        },
        {
            "label": "Organization",
            "description": "A named organisation, team or office.",
            "properties": [{"name": "name", "type": "STRING", "description": "Display name."}],
        },
    ],
    "relations": [
        {
            "label": "AFFILIATED_WITH",
            "description": "A person is affiliated with an organisation.",
            "patterns": [["Person", "Organization"]],
            "properties": [],
        }
    ],
}


class OfflineSdkLLM(LLMInterface):
    """Scripted replies for the SDK prompts this loader sends. Not a hosted model."""

    def __init__(self, model_name: str = "offline-kg") -> None:
        super().__init__(model_name=model_name, max_concurrency=1)

    def invoke(self, prompt: str, **kwargs: Any) -> LLMResponse:
        return LLMResponse(content=_reply(prompt))


def _reply(prompt: str) -> str:
    if "You are an expert named entity recognition system." in prompt:
        return json.dumps(_entities(_section(prompt, "Text"), _types(prompt)))
    if "You are an expert knowledge graph builder." in prompt:
        return json.dumps(_relations(prompt))
    if "main_entities" in prompt and "aboutness" in prompt and "central concrete" in prompt:
        names = _names(_untrusted(prompt))
        return json.dumps({"main_entities": names[:8], "aboutness": "A short organisational note."})
    if "Normalize the draft ontology" in prompt or '"title": "NormalizedDraft"' in prompt:
        return json.dumps(_ONTOLOGY)
    if "Propose the entity types" in prompt or '"title": "ChunkProposal"' in prompt:
        return json.dumps(_ONTOLOGY)
    if re.search(r"\bYES\b", prompt) and re.search(r"\bNO\b", prompt):
        return "NO"
    return "[]"


def _section(prompt: str, header: str) -> str:
    marker = f"## {header}\n"
    start = prompt.find(marker)
    if start < 0:
        return ""
    rest = prompt[start + len(marker) :]
    nxt = rest.find("\n## ")
    return rest if nxt < 0 else rest[:nxt]


def _untrusted(prompt: str) -> str:
    start = prompt.find("<<<UNTRUSTED INPUT>>>")
    end = prompt.find("<<<END UNTRUSTED INPUT>>>")
    if start < 0 or end < 0:
        return prompt
    return prompt[start + len("<<<UNTRUSTED INPUT>>>") : end]


def _types(prompt: str) -> list[str]:
    block = _section(prompt, "Entity Types")
    found = re.findall(r"[A-Za-z][A-Za-z0-9_]+", block)
    skip = {"Only", "extract", "entities", "of", "these", "types"}
    return [item for item in found if item not in skip] or ["Person", "Organization"]


def _names(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for match in _PROPER.finditer(text):
        name = match.group(0)
        if name not in seen and 2 <= len(name) <= 80:
            seen.add(name)
            out.append(name)
    return out


def _kind(name: str, types: list[str]) -> str:
    tail = name.rsplit(" ", 1)[-1].lower()
    if tail in _ORG_TAIL and "Organization" in types:
        return "Organization"
    if "Person" in types:
        return "Person"
    return types[0]


def _entities(text: str, types: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name in _names(text):
        start = text.find(name)
        out.append(
            {
                "name": name,
                "type": _kind(name, types),
                "description": f"{name} is named in the text.",
                "confidence": 0.95,
                "start": max(start, 0),
                "end": max(start, 0) + len(name),
            }
        )
    return out


def _relations(prompt: str) -> dict[str, Any]:
    text = _section(prompt, "Text")
    types = _types(prompt)
    entities = _entities(text, types)
    rels = [item for item in _REL_TYPE.findall(prompt) if "_" in item]
    rel_type = "AFFILIATED_WITH" if "AFFILIATED_WITH" in rels else (rels[0] if rels else "AFFILIATED_WITH")
    people = [e["name"] for e in entities if e["type"] == "Person"]
    orgs = [e["name"] for e in entities if e["type"] == "Organization"]
    relationships: list[dict[str, Any]] = []
    for person, org in zip(people, orgs, strict=False):
        start = text.find(person)
        relationships.append(
            {
                "source": person,
                "target": org,
                "type": rel_type,
                "description": f"{person} is affiliated with {org}.",
                "span_start": max(start, 0),
                "span_end": len(text),
            }
        )
    return {
        "entities": [{"name": e["name"], "type": e["type"], "description": e["description"]} for e in entities],
        "relationships": relationships,
    }
