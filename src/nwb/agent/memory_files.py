"""USER.md / MEMORY.md (Hermes, OpenClaw): per-user markdown files under ``/memories/`` in the LangGraph store.

They are created on first contact from ``agent/templates`` with directory data (so the agent never starts blind),
then owned by the agent: it edits them with the built-in file tools, and deepagents' ``MemoryMiddleware`` loads them
once per thread (a frozen per-session snapshot, as Hermes does, so mid-session edits do not churn the prompt).
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

from deepagents.backends.store import StoreBackend
from jinja2 import Environment, StrictUndefined
from langgraph.store.base import BaseStore

from nwb.db.repo_directory import DirectoryReader
from nwb.obs.log import Hop, dataflow
from nwb.ports import SqlDatabase

FILES = ("USER.md", "MEMORY.md")
DIRECTORY_START = "<!-- nwb:directory -->"
DIRECTORY_END = "<!-- /nwb:directory -->"
_DIRECTIVE = re.compile(r"^(?:[-*]\s+)?(?:Always|Never|Prefer)\b")


def answers_heading(template: str) -> str:
    """The first markdown heading after the directory block in the USER.md template."""
    after = template.split(DIRECTORY_END, 1)[1] if DIRECTORY_END in template else template
    for line in after.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped
    return ""


class MemoryFiles:
    def __init__(self, store: BaseStore, db: SqlDatabase, templates_dir: Path) -> None:
        self._store, self._db, self._dir = store, db, templates_dir
        self._env = Environment(undefined=StrictUndefined, keep_trailing_newline=True, autoescape=False)  # noqa: S701 - markdown files, not HTML
        self._answers = answers_heading((templates_dir / "USER.md").read_text(encoding="utf-8"))

    def _backend(self, company_id: str, user_id: str) -> StoreBackend:
        ns = (company_id, user_id, "memories")
        return StoreBackend(namespace=lambda _rt: ns, store=self._store)

    def read(self, company_id: str, user_id: str, name: str) -> str | None:
        res = self._backend(company_id, user_id).read(f"/{name}", limit=100_000)
        if res.error or res.file_data is None:
            return None
        content: Any = res.file_data.get("content")
        return "\n".join(content) if isinstance(content, list) else str(content)

    def write(self, company_id: str, user_id: str, name: str, content: str) -> None:
        """Overwrite one ``/memories/`` file (approved patch, learned directive). The caller has already run the
        profile gate. Does not delete."""
        if name not in FILES:
            raise ValueError(f"unknown memory file {name}")
        res = self._backend(company_id, user_id).write(f"/{name}", content)
        if res.error:
            raise RuntimeError(f"could not write /memories/{name}: {res.error}")

    def ensure(self, company_id: str, user_id: str, asof: date) -> list[str]:
        """Create any missing file from its template. Returns the files created.

        An existing USER.md keeps its directives; only the directory block is refreshed when HR data changed.
        """
        backend = self._backend(company_id, user_id)
        missing = [n for n in FILES if self.read(company_id, user_id, n) is None]
        values = self._values(user_id, asof)
        for name in missing:
            res = backend.write(f"/{name}", self._render(name, values))
            if res.error:
                raise RuntimeError(f"could not seed /memories/{name}: {res.error}")
        if "USER.md" not in missing:
            self._sync_directory(company_id, user_id, values)
        return missing

    def _render(self, name: str, values: dict[str, Any]) -> str:
        return self._env.from_string((self._dir / name).read_text(encoding="utf-8")).render(**values)

    def _sync_directory(self, company_id: str, user_id: str, values: dict[str, Any]) -> None:
        """Replace the HR block when directory data changed. Directives outside it stay."""
        current = self.read(company_id, user_id, "USER.md")
        fresh = directory_inner(self._render("USER.md", values))
        if current is None or fresh is None:
            return
        merged = merge_directory_block(current, fresh, self._answers)
        if merged == current:
            return
        res = self._backend(company_id, user_id).write("/USER.md", merged)
        if res.error:
            raise RuntimeError(f"could not refresh /memories/USER.md: {res.error}")
        dataflow(Hop.MEMORY_WRITE, kind="user_md_directory", person=user_id)

    def _values(self, user_id: str, asof: date) -> dict[str, Any]:
        with self._db.unit_of_work() as uow:
            d = DirectoryReader(uow)
            p = d.person(user_id)
            mgr = d.manager(user_id, asof)
            units = [m.unit_name for m in d.memberships(user_id, asof)]
        if p is None:
            raise LookupError(f"person {user_id} not in directory")
        return {
            "person_id": p.id,
            "name": p.name,
            "title": p.title,
            "grade": p.grade,
            "location": p.location,
            "hired_on": p.hired_on.isoformat() if p.hired_on else None,
            "units": ", ".join(units),
            "manager": f"{mgr.name} ({mgr.id})" if mgr else None,
            "asof": asof.isoformat(),
        }


def directory_inner(text: str) -> str | None:
    if DIRECTORY_START not in text or DIRECTORY_END not in text:
        return None
    return text.split(DIRECTORY_START, 1)[1].split(DIRECTORY_END, 1)[0].strip("\n")


def merge_directory_block(existing: str, new_inner: str, answers_heading: str) -> str:
    """Swap the directory block. ``Always`` / ``Never`` / ``Prefer`` lines inside the old block are kept.

    ``answers_heading`` is the heading from the USER.md template. Directives that were inside the old block
    are parked under it.
    """
    if DIRECTORY_START not in existing or DIRECTORY_END not in existing:
        return existing
    new_inner = new_inner.strip("\n")
    pre, rest = existing.split(DIRECTORY_START, 1)
    old_inner, post = rest.split(DIRECTORY_END, 1)
    kept = [
        line.strip()
        for line in old_inner.splitlines()
        if _DIRECTIVE.match(line.strip()) and line.strip() not in new_inner and line.strip() not in post
    ]
    if kept:
        post = _park_directives(post, kept, answers_heading)
    return f"{pre}{DIRECTORY_START}\n{new_inner}\n{DIRECTORY_END}{post}"


def _park_directives(post: str, lines: list[str], heading: str) -> str:
    block = "\n".join(lines)
    if heading and heading in post:
        return post.replace(heading, f"{heading}\n{block}", 1)
    suffix = "" if post.endswith("\n") else "\n"
    return f"{post}{suffix}{block}\n"
