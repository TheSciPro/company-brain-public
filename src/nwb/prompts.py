"""Versioned prompt files: ``prompts/<name>.md`` = YAML frontmatter + Jinja2 body.

Frontmatter keys: ``name``, ``version`` (int), ``description``, optional ``inputs`` (list of required variables).
Rendering uses ``StrictUndefined`` so a missing variable is an error, never an empty string. The rendered
result carries ``name@version`` so every LLM call can be traced back to the exact prompt revision.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, StrictUndefined, TemplateError

from nwb.errors import PromptError


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    name: str
    version: int
    description: str
    inputs: tuple[str, ...]
    body: str
    meta: dict[str, Any]

    @property
    def ref(self) -> str:
        return f"{self.name}@v{self.version}"


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    ref: str
    text: str


def parse_frontmatter(raw: str, source: str) -> tuple[dict[str, Any], str]:
    if not raw.startswith("---"):
        raise PromptError(f"{source}: missing YAML frontmatter")
    try:
        _, fm, body = raw.split("---", 2)
    except ValueError as exc:
        raise PromptError(f"{source}: unterminated frontmatter") from exc
    meta = yaml.safe_load(fm) or {}
    if not isinstance(meta, dict):
        raise PromptError(f"{source}: frontmatter must be a mapping")
    return meta, body.lstrip("\n")


class PromptLibrary:
    """Loads templates lazily from a directory; nested names use '/' (e.g. ``tools/search_memory``)."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._env = Environment(undefined=StrictUndefined, autoescape=False, trim_blocks=True, lstrip_blocks=True)  # noqa: S701  # nosec: plain-text prompts, not HTML
        self._cache: dict[str, PromptTemplate] = {}

    def get(self, name: str) -> PromptTemplate:
        if name not in self._cache:
            path = self._root / f"{name}.md"
            if not path.is_file():
                raise PromptError(f"prompt {name!r} not found at {path}")
            meta, body = parse_frontmatter(path.read_text(encoding="utf-8"), str(path))
            if "version" not in meta:
                raise PromptError(f"{path}: frontmatter needs a 'version'")
            self._cache[name] = PromptTemplate(
                name=str(meta.get("name", name)),
                version=int(meta["version"]),
                description=str(meta.get("description", "")),
                inputs=tuple(meta.get("inputs") or ()),
                body=body,
                meta=meta,
            )
        return self._cache[name]

    def render(self, prompt_name: str, /, **variables: Any) -> RenderedPrompt:
        """Render ``prompts/<prompt_name>.md``; every declared input must be passed (StrictUndefined)."""
        tpl = self.get(prompt_name)
        missing = [k for k in tpl.inputs if k not in variables]
        if missing:
            raise PromptError(f"{tpl.ref}: missing inputs {missing}")
        try:
            text = self._env.from_string(tpl.body).render(**variables)
        except TemplateError as exc:
            raise PromptError(f"{tpl.ref}: {exc}") from exc
        return RenderedPrompt(ref=tpl.ref, text=text.strip())

    def description(self, name: str) -> str:
        """Body of a static prompt (e.g. a tool description), rendered with no variables."""
        return self.render(name).text
