"""Stub Graphiti LLM for the team-move proof. Not a live model.

It extracts "works on the X team" / "moved to the Y team" and marks the old team fact contradicted
when the new fact names a different team. Recorded cassettes are not required for this path.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any

from graphiti_core.llm_client.client import LLMClient
from graphiti_core.llm_client.config import LLMConfig
from pydantic import BaseModel

_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")
_TEAM = re.compile(
    r"\b(?:work(?:s|ing)? on|moved to|moving to) the ([A-Za-z][A-Za-z0-9]+) (?:team|office)\b",
    re.IGNORECASE,
)
_FROM_TO = re.compile(
    r"\bmoved from the ([A-Za-z][A-Za-z0-9]+) team to the ([A-Za-z][A-Za-z0-9]+) team\b",
    re.IGNORECASE,
)


class TeamMoveLLM(LLMClient):
    """Deterministic stand-in. ``_generate_response`` never calls a provider."""

    def __init__(self) -> None:
        super().__init__(
            LLMConfig(api_key="stub", model="team-move-stub", small_model="team-move-stub", max_tokens=2048)
        )

    async def _generate_response(
        self,
        messages: list[Any],
        response_model: type[BaseModel] | None = None,
        max_tokens: int = 2048,
        model_size: Any = None,
        *,
        model: str | None = None,
    ) -> dict[str, Any]:
        text = _body(messages)
        if response_model is None:
            return {}
        name = response_model.__name__
        if name == "ExtractedEntities":
            return {"extracted_entities": _entities(text)}
        if name == "NodeResolutions":
            return {"entity_resolutions": _resolutions(text)}
        if name == "ExtractedEdges":
            return {"edges": _edges(text)}
        if name == "EdgeDuplicate":
            return _duplicate(text)
        if name in {"EdgeTimestamps", "BatchEdgeTimestamps"}:
            return _timestamps(text, name)
        if name == "SummarizedEntities":
            return {"summaries": []}
        if name in {"EntitySummary", "Summary"}:
            return {"summary": "Named entity from the user's message."}
        if name == "SummaryDescription":
            return {"description": "A dated work fact."}
        return _defaults(response_model)


def _body(messages: list[Any]) -> str:
    raw = "\n".join(str(getattr(m, "content", m)) for m in messages)
    return raw.split("Respond with a JSON object", 1)[0]


def _section(text: str, start: str, end: str) -> str:
    match = re.search(re.escape(start) + r"\s*(.*?)\s*" + re.escape(end), text, re.DOTALL)
    return match.group(1).strip() if match else ""


def _current(text: str) -> str:
    for label in ("CURRENT_MESSAGE", "CURRENT MESSAGE"):
        chunk = _section(text, f"<{label}>", f"</{label}>")
        if chunk:
            return chunk
    return text


def _entities(text: str) -> list[dict[str, Any]]:
    message = _current(text)
    names = ["User"]
    moved = _FROM_TO.search(message)
    if moved:
        names.extend([moved.group(1), moved.group(2)])
    else:
        names.extend(match.group(1) for match in _TEAM.finditer(message))
    out = []
    seen: set[str] = set()
    for name in names:
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        type_id = 1 if name == "User" else 2
        out.append({"name": name, "entity_type_id": type_id, "episode_indices": [0]})
    return out


def _json_block(text: str, start: str, end: str) -> list[Any]:
    """Graphiti renders candidate facts with ``repr`` (single quotes), not JSON."""
    raw = _section(text, start, end)
    if not raw:
        return []
    for parser in (json.loads, ast.literal_eval):
        try:
            data = parser(raw)
        except (json.JSONDecodeError, SyntaxError, ValueError):
            continue
        return data if isinstance(data, list) else []
    return []


def _resolutions(text: str) -> list[dict[str, Any]]:
    entities = _json_block(text, "<ENTITIES>", "</ENTITIES>")
    existing = _json_block(text, "<EXISTING ENTITIES>", "</EXISTING ENTITIES>")
    by_name = {
        str(item.get("name", "")).casefold(): int(item.get("candidate_id", -1))
        for item in existing
        if isinstance(item, dict)
    }
    resolutions = []
    for item in entities:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", ""))
        resolutions.append(
            {
                "id": int(item.get("id", 0)),
                "name": name,
                "duplicate_candidate_id": by_name.get(name.casefold(), -1),
            }
        )
    return resolutions


def _edges(text: str) -> list[dict[str, Any]]:
    message = _current(text)
    moved = _FROM_TO.search(message)
    if moved:
        src, dst = moved.group(1), moved.group(2)
        fact = f"User moved from the {src} team to the {dst} team"
        return [_edge("User", dst, "MOVED_TO", fact)]
    found = list(_TEAM.finditer(message))
    if not found:
        return []
    team = found[-1].group(1)
    lowered = message.lower()
    relation = "WORKS_ON" if "work" in lowered else "MOVED_TO"
    verb = "works on" if relation == "WORKS_ON" else "moved to"
    return [_edge("User", team, relation, f"User {verb} the {team} team")]


def _edge(source: str, target: str, relation: str, fact: str) -> dict[str, Any]:
    return {
        "source_entity_name": source,
        "target_entity_name": target,
        "relation_type": relation,
        "fact": fact,
        "valid_at": None,
        "invalid_at": None,
        "episode_indices": [0],
    }


def _duplicate(text: str) -> dict[str, list[int]]:
    existing = _json_block(text, "<EXISTING FACTS>", "</EXISTING FACTS>")
    invalidation = _json_block(text, "<FACT INVALIDATION CANDIDATES>", "</FACT INVALIDATION CANDIDATES>")
    new = _section(text, "<NEW FACT>", "</NEW FACT>")
    contradicted: list[int] = []
    if re.search(r"\bplatform\b", new, re.IGNORECASE):
        for item in [*existing, *invalidation]:
            if not isinstance(item, dict):
                continue
            fact = str(item.get("fact", ""))
            if re.search(r"\bpayments\b", fact, re.IGNORECASE) and re.search(r"\bwork", fact, re.IGNORECASE):
                contradicted.append(int(item["idx"]))
    return {"duplicate_facts": [], "contradicted_facts": contradicted}


def _timestamps(text: str, name: str) -> dict[str, Any]:
    found = _ISO.findall(text)
    valid = found[-1] if found else None
    if name == "BatchEdgeTimestamps":
        return {"timestamps": [{"valid_at": valid, "invalid_at": None}]}
    return {"valid_at": valid, "invalid_at": None}


def _defaults(model: type[BaseModel]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for fname, field in model.model_fields.items():
        if not field.is_required():
            continue
        origin = getattr(field.annotation, "__origin__", None)
        if field.annotation is str:
            values[fname] = ""
        elif origin is list or str(field.annotation).startswith("list"):
            values[fname] = []
        elif field.annotation is int:
            values[fname] = 0
        elif field.annotation is bool:
            values[fname] = False
        else:
            values[fname] = None
    return model.model_validate(values).model_dump()
