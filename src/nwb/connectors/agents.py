"""Build one Deep Agents subagent per source, with a read-only ``query_*`` tool and, where the catalog
sets ``writes:``, an ``update_*`` tool that pauses for approval and refuses every other path.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from deepagents import FilesystemPermission
from deepagents.backends.protocol import BackendProtocol
from deepagents.backends.utils import validate_path
from deepagents.middleware.filesystem import _check_fs_permission
from langchain.agents.middleware import InterruptOnConfig
from langchain.tools import ToolRuntime, tool
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool, ToolException
from pydantic import BaseModel, ConfigDict

from nwb.agent.subagents import SubAgent, SubagentFile, materialize_subagent
from nwb.agent.tools.base import Result, context, domain_errors, finish, result
from nwb.conf.models import ConnectorDecl, InterruptRule, SearchConfig, SourcesConfig
from nwb.connectors.catalog import SourceSpec, list_sources, tool_name
from nwb.connectors.ports import ActionSink, ComposioClient
from nwb.connectors.read import ReadOnlySource, SourceRead
from nwb.errors import ConfigError
from nwb.obs.log import Hop, dataflow, get_logger
from nwb.ports import AccessPolicy, BlobStore, SqlDatabase

log = get_logger(__name__)

# One call to read, one to answer, and one spare if the model queries twice.
SOURCE_MODEL_CALLS = 4
_APPROVAL = InterruptRule(allowed_decisions=("approve", "reject"))


class SourceUpdate(BaseModel):
    model_config = ConfigDict(frozen=True)
    source: str
    path: str
    status: Literal["done"]
    external_ref: str


@dataclass
class ConnectorBindings:
    """Injected client and sink. Tests replace ``sink``; production leaves both empty until a client is supplied."""

    client: ComposioClient | None = None
    sink: ActionSink | None = None

    def sink_for(self, spec: SourceSpec) -> ActionSink | None:
        if self.sink is not None:
            return self.sink
        conn = spec.connector
        if self.client is None or conn is None or not conn.update_action:
            return None
        return ClientActionSink(self.client, conn, name=spec.id)


class ClientActionSink:
    """``ActionSink`` that runs the connector's ``update_action``. Constructed only for an approved update.

    Arguments are the action's own: ``update_target_param`` gets the target, ``update_text_param`` the text.
    """

    def __init__(self, client: ComposioClient, conn: ConnectorDecl, name: str) -> None:
        if not conn.update_action:
            raise ConfigError(f"connector {conn.id!r} has no update_action")
        self._client = client
        self._conn = conn
        self._action = conn.update_action
        self.name = name

    def execute(self, kind: str, target_ref: str, title: str, message: str) -> str | None:
        params = {self._conn.update_target_param: target_ref, self._conn.update_text_param: message}
        return created_ref(self._client.execute(self._action, params), self._conn.update_ref_keys)


def created_ref(data: Mapping[str, Any], keys: Sequence[str]) -> str | None:
    """The created item's id from an action response, at the top level or under ``data``. None when absent."""
    nested = data.get("data")
    for layer in (data, nested if isinstance(nested, Mapping) else {}):
        for key in keys:
            value = layer.get(key)
            if value not in (None, ""):
                return str(value)
    return None


def post_target(spec: SourceSpec, path: str) -> str | None:
    """The approved path's last segment, when it is one of the connector's ``post_targets``."""
    conn = spec.connector
    if conn is None:
        return None
    try:
        normalized = validate_path(path)
    except ValueError:
        return None
    target = normalized.rstrip("/").rsplit("/", 1)[-1]
    return target if target in conn.post_targets else None


@dataclass(frozen=True)
class SourceAgents:
    specs: tuple[SourceSpec, ...]
    subagents: tuple[SubAgent, ...]
    interrupts: Mapping[str, InterruptRule]
    tools: Mapping[str, BaseTool] = field(default_factory=dict)


def path_allowed(path: str, writes: Sequence[str]) -> bool:
    """True only when ``path`` matches the source's ``writes:`` allowlist. Empty allowlist denies everything."""
    if not writes:
        return False
    try:
        normalized = validate_path(path)
    except ValueError:
        return False
    rules = [
        FilesystemPermission(operations=["write"], paths=list(writes), mode="allow"),
        FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
    ]
    return _check_fs_permission(rules, "write", normalized) == "allow"


def build_source_agents(
    cfg: SourcesConfig,
    db: SqlDatabase,
    policy: AccessPolicy,
    blobs: BlobStore,
    bindings: ConnectorBindings,
    terms: SearchConfig,
    *,
    model_for: Callable[[Literal["main", "background"]], BaseChatModel],
    backend: BackendProtocol,
    parent_permissions: Sequence[FilesystemPermission],
    shared_middleware: Sequence[Any] = (),
) -> SourceAgents:
    specs = tuple(list_sources(cfg))
    tools: dict[str, BaseTool] = {}
    subagents: list[SubAgent] = []
    interrupts: dict[str, InterruptRule] = {}
    for spec in specs:
        view = ReadOnlySource(spec, db, policy, blobs, terms, lambda: bindings.client)
        bound = [make_query_tool(spec, view)]
        interrupt: dict[str, InterruptOnConfig] | None = None
        if spec.writes and spec.update_tool:
            update = make_update_tool(spec, bindings, gated=True)
            bound.append(update)
            interrupts[spec.update_tool] = _APPROVAL
            interrupt = {spec.update_tool: InterruptOnConfig(allowed_decisions=["approve", "reject"])}
        for bound_tool in bound:
            if bound_tool.name in tools:
                raise ConfigError(f"duplicate source tool {bound_tool.name}")
            tools[bound_tool.name] = bound_tool
        file = SubagentFile(
            name=spec.agent_name,
            description=_description(spec),
            model="background",
            model_call_limit=SOURCE_MODEL_CALLS,
            writes=spec.writes,
        )
        subagents.append(
            materialize_subagent(
                file,
                _prompt(spec),
                bound,
                model_for("background"),
                shared_middleware,
                backend=backend,
                parent_permissions=parent_permissions,
                interrupt_on=interrupt,
            )
        )
    log.info("source subagents: %s", [spec.agent_name for spec in specs])
    return SourceAgents(specs, tuple(subagents), interrupts, tools)


def make_query_tool(spec: SourceSpec, view: ReadOnlySource) -> BaseTool:
    """A tool closed over a read-only view. It cannot reach an ``ActionSink``."""

    def query_source(question: str, runtime: ToolRuntime[Any, Any], limit: int = 6) -> Result:
        if len(question.strip()) < 2:
            raise ToolException("query too short")
        found: SourceRead = view.search(context(runtime).actor(), question, max(1, min(limit, 8)))
        return result(found)

    query_source.__doc__ = (
        f"Search only the {spec.label} source for records this user may see. This connection is read-only.\n\n"
        "Args:\n"
        "    question: words to look for.\n"
        "    limit: maximum hits to return.\n"
    )
    wrapped = tool(spec.query_tool, response_format="content_and_artifact", parse_docstring=True)(
        domain_errors(query_source)
    )
    return finish(wrapped)


def make_update_tool(spec: SourceSpec, bindings: ConnectorBindings, *, gated: bool = False) -> BaseTool:
    """Update tool. ``gated`` must be true only when the tool is also listed in ``interrupt_on``.

    The default is closed: without that registration the tool refuses, and a path outside ``writes``
    refuses even after a person approves.
    """
    name = spec.update_tool or tool_name("update", spec.id)

    def update_source(path: str, message: str, runtime: ToolRuntime[Any, Any], title: str = "") -> Result:
        context(runtime)  # identity still comes from the runtime; the schema does not carry it
        if not gated:
            _refuse(spec, "approval", path)
            raise ToolException("refused: update requires human approval")
        if not path_allowed(path, spec.writes):
            _refuse(spec, "writes", path)
            raise ToolException("refused: path is outside this source's writes allowlist")
        target = post_target(spec, path)
        if target is None:
            _refuse(spec, "target", path)
            raise ToolException("refused: the path does not name an allowed target for this source")
        sink = bindings.sink_for(spec)
        if sink is None:
            _refuse(spec, "not_connected", path)
            raise ToolException("refused: this source is not connected")
        ref = sink.execute(spec.update_kind, target, title, message)
        if not ref:
            _refuse(spec, "unconfirmed", path)
            raise ToolException("failed: the app returned no id for the update, so it is not reported as done")
        dataflow(Hop.TOOL_CALL, tool=name, outcome="executed", path=path, source=spec.id)
        return result(SourceUpdate(source=spec.id, path=path, status="done", external_ref=ref))

    update_source.__doc__ = (
        f"Update the {spec.label} source. A person must approve this call before it runs, "
        f"and the path must be on the writes allowlist ({', '.join(spec.writes) or 'none'}).\n\n"
        "Args:\n"
        "    path: virtual path ending in the target id, e.g. /sources/<source>/<target id>. Anything outside the "
        "allowlist or not an allowed target is refused.\n"
        "    message: the text to send.\n"
        "    title: short label for the update.\n"
    )
    wrapped = tool(name, response_format="content_and_artifact", parse_docstring=True)(domain_errors(update_source))
    return finish(wrapped, "external")


def _refuse(spec: SourceSpec, outcome: str, path: str) -> None:
    log.info("source update refused", source=spec.id, outcome=outcome)
    dataflow(Hop.TOOL_CALL, tool=spec.update_tool or spec.id, outcome=outcome, path=path, source=spec.id)


def _description(spec: SourceSpec) -> str:
    head = spec.connector.description if spec.connector else f"Source subagent for {spec.label}."
    if spec.writes:
        tail = " Updates wait for approval and only the declared writes paths are allowed."
    else:
        tail = " Read-only."
    text = f"{head} Delegate questions about this source here. It reads with {spec.query_tool}.{tail}"
    return text if len(text) >= 10 else f"{text} Use the task tool."


def _prompt(spec: SourceSpec) -> str:
    lines = [
        f"You answer from the {spec.label} source only.",
        f"Call {spec.query_tool} once with the question, then answer. Do not call it again.",
        "Cite each id you use in square brackets. Prefer an approved or current record over a draft or superseded one.",
        "If it returns nothing, say this source has no matching record. Do not guess and do not use another source.",
    ]
    if spec.writes and spec.update_tool:
        lines.append(
            f"Changes go through {spec.update_tool}. That call pauses until a person approves it. "
            f"The path must be one of: {', '.join(spec.writes)}, and its last segment is the target. "
            "Any other path or target is refused."
        )
    else:
        lines.append("This source is read-only. Do not try to update it.")
    return "\n".join(lines)
