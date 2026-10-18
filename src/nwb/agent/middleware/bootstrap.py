"""One onboarding beat per chat turn (OpenClaw task-first ritual, Hermes beats).

Which users get one, and what it says, is decided by the injected ``BootstrapSource``. The source also decides
which run purposes onboard (heartbeat and cron do not). A finished run marks the beat shown only when the
final reply ends with an empty ``<onboarding-beat id="..."/>`` marker. A tag with a body, or an unclosed tag,
is the model pasting private guidance: it is scrubbed and does not count. A marker on a tool-call step does
not publish the beat, because that text is not the reply. A failed run never gets here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Annotated, Any, NotRequired, Protocol

from langchain.agents.middleware import AgentMiddleware, AgentState, ModelRequest, ModelResponse
from langchain.agents.middleware.types import PrivateStateAttr
from langchain_core.messages import AIMessage, BaseMessage

from nwb.agent.context import RuntimeContext
from nwb.agent.middleware._util import append_system, messages_this_run, run_context, state_of
from nwb.onboarding.marker import offered_ids, visible_reply


class BootstrapNote(Protocol):
    beat_id: str
    text: str


class BootstrapSource(Protocol):
    def note(self, ctx: RuntimeContext) -> BootstrapNote | None: ...

    def offered(self, ctx: RuntimeContext, beat_id: str) -> None: ...


class BootstrapState(AgentState):
    nwb_bootstrap: NotRequired[Annotated[dict[str, str], PrivateStateAttr]]


class BootstrapMiddleware(AgentMiddleware[BootstrapState, RuntimeContext, Any]):
    state_schema = BootstrapState

    def __init__(self, source: BootstrapSource) -> None:
        super().__init__()
        self._source = source

    @property
    def name(self) -> str:
        return "nwb_bootstrap"

    def before_agent(self, state: BootstrapState, runtime: Any) -> dict[str, Any] | None:
        del state
        ctx = run_context(runtime)
        if ctx is None:
            return None
        note = self._source.note(ctx)
        if note is None:
            return None
        return {"nwb_bootstrap": {"run": ctx.run_id, "beat": note.beat_id, "text": note.text}}

    def after_agent(self, state: BootstrapState, runtime: Any) -> dict[str, Any] | None:
        ctx = run_context(runtime)
        shown = state.get("nwb_bootstrap") or {}
        beat = shown.get("beat")
        if ctx is None or shown.get("run") != ctx.run_id or not isinstance(beat, str):
            return None
        produced = messages_this_run(list(state.get("messages") or []))
        if not beat_landed(produced, beat):
            return None
        self._source.offered(ctx, beat)
        return None

    def _apply(self, request: ModelRequest[RuntimeContext]) -> ModelRequest[RuntimeContext]:
        ctx = run_context(request.runtime)
        shown = state_of(request).get("nwb_bootstrap") or {}
        if ctx is None or shown.get("run") != ctx.run_id:
            return request
        return append_system(request, f"<bootstrap>\n{shown.get('text', '')}\n</bootstrap>")

    def wrap_model_call(
        self,
        request: ModelRequest[RuntimeContext],
        handler: Callable[[ModelRequest[RuntimeContext]], ModelResponse[Any]],
    ) -> ModelResponse:
        prepared = self._apply(request)
        return _scrub_response(handler(prepared), _current_beat(prepared))

    async def awrap_model_call(
        self,
        request: ModelRequest[RuntimeContext],
        handler: Callable[[ModelRequest[RuntimeContext]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        prepared = self._apply(request)
        return _scrub_response(await handler(prepared), _current_beat(prepared))


def beat_landed(messages: Sequence[BaseMessage], beat_id: str) -> bool:
    """The final reply's empty marker names this beat. A tool-call step is not that reply."""
    reply = next((msg for msg in reversed(messages) if isinstance(msg, AIMessage) and not msg.tool_calls), None)
    if reply is None:
        return False
    return reply.additional_kwargs.get("nwb_offered_beat") == beat_id or beat_id in offered_ids(_text(reply))


def _current_beat(request: ModelRequest[RuntimeContext]) -> str | None:
    beat = state_of(request).get("nwb_bootstrap", {}).get("beat")
    return beat if isinstance(beat, str) else None


def _scrub_response(response: ModelResponse[Any], beat_id: str | None) -> ModelResponse[Any]:
    """Store the natural reply. Remember an empty marker without leaving it in the text."""
    response.result = [_scrub_message(msg, beat_id) if isinstance(msg, AIMessage) else msg for msg in response.result]
    return response


def _scrub_message(msg: AIMessage, beat_id: str | None) -> AIMessage:
    raw = _text(msg)
    kwargs = dict(msg.additional_kwargs)
    # Only the final reply (no tool calls) can publish the beat. The runner discards tool-step text.
    if beat_id and not msg.tool_calls and beat_id in offered_ids(raw):
        kwargs["nwb_offered_beat"] = beat_id
    cleaned = visible_reply(raw)
    if cleaned == raw and kwargs == msg.additional_kwargs:
        return msg
    return msg.model_copy(update={"content": cleaned, "additional_kwargs": kwargs})


def _text(msg: AIMessage) -> str:
    content = msg.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content)
    return str(content)
