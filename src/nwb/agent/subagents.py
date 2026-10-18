"""Subagents declared in markdown (``agent/subagents/*.md``), deepagents ``SubAgent`` specs.

Frontmatter: ``name``, ``description`` (what the main agent sees for ``task``), ``tools`` (names from our
tool registry), ``model`` (``main`` / ``background``), ``model_call_limit``. The body is the system prompt.

Filesystem writes fail closed. Deep Agents attaches ``write_file`` and ``edit_file`` to every subagent
unless that subagent's own ``FilesystemMiddleware`` says otherwise, and a ``permissions`` list on the
spec replaces the parent's rules. This loader therefore:

- defaults ``read_only`` to true. The key may be omitted. ``read_only: true`` is accepted and redundant;
- with no ``writes`` allowlist, passes a middleware whose tool allowlist is the read tools only, denies
  every write with ``FilesystemPermission``, and wraps the backend in ``WritePolicyBackend``;
- with ``writes:`` (virtual path globs, same syntax as ``FilesystemPermission``), exposes ``write_file``
  and ``edit_file`` but the permission rules and the backend allow only those paths. Parent write-denies
  are copied first, so an allowlist cannot reopen ``/org/**`` or ``/skills/**``;
- with ``read_only: false`` and no ``writes``, inherits the main agent's filesystem tools and permissions
  (the only way to get general writes).

Prompt text is not the guard.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import yaml
from deepagents import FilesystemPermission, SubAgent
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemMiddleware, FsToolName
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from nwb.agent.backends import WritePolicyBackend
from nwb.errors import ConfigError

# ``read_file`` is required by FilesystemMiddleware. ``delete`` and ``execute`` stay off.
_READ_FS_TOOLS: tuple[FsToolName, ...] = ("ls", "read_file", "glob", "grep")
_WRITE_FS_TOOLS: tuple[FsToolName, ...] = ("ls", "read_file", "write_file", "edit_file", "glob", "grep")


class SubagentFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    description: str = Field(min_length=10)
    tools: tuple[str, ...] = ()
    model: Literal["main", "background"] = "main"
    model_call_limit: int = Field(4, ge=1, le=20)
    read_only: bool = True
    writes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _writes_are_permission_globs(self) -> SubagentFile:
        if not self.writes:
            return self
        try:
            FilesystemPermission(operations=["write"], paths=list(self.writes), mode="allow")
        except (ValueError, NotImplementedError) as exc:
            raise ValueError(f"writes: {exc}") from exc
        return self


def split_markdown(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        raise ConfigError("subagent file needs YAML frontmatter")
    _, fm, body = text.split("---", 2)
    meta = yaml.safe_load(fm) or {}
    if not isinstance(meta, dict):
        raise ConfigError("subagent frontmatter must be a mapping")
    return meta, body.strip()


def load_subagent_files(directory: Path) -> list[tuple[SubagentFile, str]]:
    out = []
    for path in sorted(directory.glob("*.md")):
        meta, body = split_markdown(path.read_text(encoding="utf-8"))
        try:
            spec = SubagentFile.model_validate(meta)
        except ValidationError as exc:
            if any((err.get("loc") or (None,))[0] == "model" for err in exc.errors()):
                raise ConfigError(
                    f"{path.name}: unknown model {meta.get('model')!r}; expected 'main' or 'background'"
                ) from exc
            raise
        if spec.name != path.stem:
            raise ConfigError(f"{path.name}: name {spec.name!r} must match the file name")
        out.append((spec, body))
    return out


def _write_rules(spec: SubagentFile, parent: Sequence[FilesystemPermission]) -> list[FilesystemPermission] | None:
    """Rules that replace the parent's, or ``None`` when the subagent inherits them.

    ``None`` is only ``read_only: false`` with no allowlist. Otherwise the list ends in a
    catch-all write deny. Parent denies come first so an allowlist cannot widen them
    (first match wins).
    """
    if not spec.read_only and not spec.writes:
        return None
    rules = [
        FilesystemPermission(operations=list(rule.operations), paths=list(rule.paths), mode="deny")
        for rule in parent
        if rule.mode == "deny"
    ]
    if spec.writes:
        rules.append(FilesystemPermission(operations=["write"], paths=list(spec.writes), mode="allow"))
    rules.append(FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"))
    return rules


def _filesystem_guard(
    spec: SubagentFile,
    backend: BackendProtocol,
    rules: list[FilesystemPermission],
) -> FilesystemMiddleware:
    """Replace the subagent's default filesystem middleware (same ``.name``, so deepagents swaps it in).

    The default middleware would expose every write tool and would use the shared backend. This one
    carries the tool allowlist, the permission rules, and a backend that enforces those rules itself.
    """
    tools = _WRITE_FS_TOOLS if spec.writes else _READ_FS_TOOLS
    guard = FilesystemMiddleware(
        backend=WritePolicyBackend(backend, rules),
        tools=list(tools),
        _permissions=rules,
    )
    # deepagents adds the ``files`` state channel only when it sees a StateBackend, and the wrapper hides
    # it. Without the channel, ``ls /`` in a subagent raised KeyError('files') and failed the whole run.
    # Take the schema deepagents derives for the wrapped backend.
    guard.state_schema = FilesystemMiddleware(backend=backend).state_schema
    return guard


def materialize_subagent(
    spec_file: SubagentFile,
    body: str,
    bound_tools: Sequence[BaseTool],
    model: BaseChatModel,
    shared_middleware: Sequence[Any] = (),
    *,
    backend: BackendProtocol,
    parent_permissions: Sequence[FilesystemPermission] = (),
    interrupt_on: Mapping[str, Any] | None = None,
) -> SubAgent:
    """One deepagents ``SubAgent`` from a parsed spec. Writes fail closed unless ``writes`` is set."""
    limit: Any = ModelCallLimitMiddleware(run_limit=spec_file.model_call_limit, exit_behavior="end")
    rules = _write_rules(spec_file, parent_permissions)
    middleware: list[Any] = []
    if rules is not None:
        middleware.append(_filesystem_guard(spec_file, backend, rules))
    middleware.extend(shared_middleware)
    middleware.append(limit)
    spec: SubAgent = {
        "name": spec_file.name,
        "description": spec_file.description,
        "system_prompt": body,
        "tools": list(bound_tools),
        "model": model,
        "middleware": middleware,
    }
    if rules is not None:
        spec["permissions"] = rules
    if interrupt_on:
        spec["interrupt_on"] = dict(interrupt_on)
    return spec


def build_subagents(
    directory: Path,
    tools: Mapping[str, BaseTool],
    model_for: Callable[[Literal["main", "background"]], BaseChatModel],
    shared_middleware: Sequence[Any] = (),
    *,
    backend: BackendProtocol,
    parent_permissions: Sequence[FilesystemPermission] = (),
) -> list[SubAgent]:
    specs: list[SubAgent] = []
    for spec_file, body in load_subagent_files(directory):
        unknown = [name for name in spec_file.tools if name not in tools]
        if unknown:
            raise ConfigError(f"subagent {spec_file.name}: unknown tools {unknown}")
        specs.append(
            materialize_subagent(
                spec_file,
                body,
                [tools[name] for name in spec_file.tools],
                model_for(spec_file.model),
                shared_middleware,
                backend=backend,
                parent_permissions=parent_permissions,
            )
        )
    return specs
