"""Minimal YAML-ish frontmatter parser for our markdown sources (``key: value``, ``[a, b]`` lists, quotes, null)."""

from __future__ import annotations

from typing import Any


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    head, body = text[3:end], text[end + 4 :].lstrip("\n")
    meta: dict[str, Any] = {}
    for line in head.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line or line.startswith((" ", "\t")):
            continue
        key, raw = line.split(":", 1)
        meta[key.strip()] = _value(raw.strip())
    return meta, body


def _value(raw: str) -> Any:
    if raw in ("", "null", "~"):
        return None
    if raw.startswith("[") and raw.endswith("]"):
        return [_scalar(p.strip()) for p in _split_list(raw[1:-1]) if p.strip()]
    return _scalar(raw)


def _scalar(raw: str) -> Any:
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return None if raw == "null" else raw


def _split_list(inner: str) -> list[str]:
    """Split on commas that are not inside parentheses or quotes."""
    parts: list[str] = []
    buf: list[str] = []
    depth, quote = 0, ""
    for ch in inner:
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return parts
