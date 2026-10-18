"""Read-only view of ``agent/skills/*/SKILL.md`` frontmatter (validation and tool binding at build time).

deepagents ``SkillsMiddleware`` does the runtime work (listing, progressive disclosure, binding). We only need to
know, at build time, which tools some skill binds, so a tool nobody can reach is a startup error.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from nwb.errors import ConfigError


def skill_frontmatter(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ConfigError(f"{path}: SKILL.md needs YAML frontmatter")
    meta = yaml.safe_load(text.split("---", 2)[1]) or {}
    if not isinstance(meta, dict) or "name" not in meta or "description" not in meta:
        raise ConfigError(f"{path}: SKILL.md frontmatter needs name and description")
    return meta


def skill_tool_names(skills_dir: Path) -> set[str]:
    names: set[str] = set()
    for path in sorted(skills_dir.glob("*/SKILL.md")):
        meta = skill_frontmatter(path)
        md = meta.get("metadata") or {}
        include = md.get("include_tools", "") if isinstance(md, dict) else ""
        names.update(str(include).split())
    return names
