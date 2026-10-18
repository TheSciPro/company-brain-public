"""Shared helpers for tool modules: context extraction, typed results, error mapping, side-effect metadata."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, Literal

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, ToolException
from pydantic import BaseModel

from nwb.agent.context import RuntimeContext
from nwb.errors import NwbError
from nwb.knowledge.models import FOR_MODEL
from nwb.obs.log import get_logger

log = get_logger(__name__)

SideEffect = Literal["none", "write_memory", "write_action", "external"]
Result = tuple[str, dict[str, Any]]


def context(runtime: ToolRuntime[Any, Any]) -> RuntimeContext:
    """The run's identity. Refuses to run without one rather than guessing."""
    ctx = runtime.context
    if not isinstance(ctx, RuntimeContext):
        raise ToolException("this tool needs the runtime context; refusing")
    return ctx


def result(model: BaseModel) -> Result:
    """``content_and_artifact``: compact JSON for the model, the structured dict for audit/UI (no re-parsing)."""
    content = model.model_dump_json(exclude_none=True, context=FOR_MODEL)  # audit-only fields stay in the artifact
    return content, model.model_dump(mode="json", exclude_none=True)


def domain_errors[**P](fn: Callable[P, Result]) -> Callable[P, Result]:
    """Map our domain errors to ``ToolException`` (shown to the model); anything else is a logged internal error."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> Result:
        try:
            return fn(*args, **kwargs)
        except ToolException:
            raise
        except NwbError as exc:
            raise ToolException(f"{type(exc).__name__}: {exc}") from exc
        except Exception as exc:  # never leak internals into the conversation
            log.exception("tool %s crashed", fn.__name__)
            raise ToolException("the tool failed unexpectedly; tell the user and do not retry") from exc

    return wrapper


def finish(t: BaseTool, side_effect: SideEffect = "none") -> BaseTool:
    """Common tool settings: errors go back to the model; side effect recorded for the audit middleware."""
    t.handle_tool_error = True
    t.metadata = {**(t.metadata or {}), "side_effect": side_effect}
    return t


def side_effect_of(t: object) -> SideEffect:
    meta = getattr(t, "metadata", None) or {}
    v = meta.get("side_effect", "none")
    return v if v in ("none", "write_memory", "write_action", "external") else "none"
