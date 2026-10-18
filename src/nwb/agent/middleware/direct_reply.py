"""End the main agent's turn after a tool that already wrote the reply (``direct_reply_tools`` in config/agent.yaml).

``company_fact`` returns a finished sentence (``say``) with ``stop``. Left to itself, the model sometimes keeps
searching after a "not on record" (several minutes of researcher calls) or makes a second call only to repeat the
sentence. This hook runs before the next model call: when every tool result since the last model message comes
from such a tool and carries ``stop`` with ``say``, the turn ends and the runner shows ``say`` as the reply.

``return_direct`` cannot do this: skill-gated tools are not in the tool node that checks it.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.runtime import Runtime


def direct_reply_of(artifact: object) -> str:
    """The finished sentence a tool asks to be shown as is (``stop`` with ``say``), or ""."""
    if isinstance(artifact, dict) and artifact.get("stop") and isinstance(artifact.get("say"), str):
        return str(artifact["say"])
    return ""


class DirectReplyMiddleware(AgentMiddleware):
    def __init__(self, tools: frozenset[str]) -> None:
        super().__init__()
        self._tools = tools

    @property
    def name(self) -> str:
        return "nwb_direct_reply"

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: AgentState[Any], runtime: Runtime[Any]) -> dict[str, Any] | None:
        results: list[ToolMessage] = []
        for m in reversed(state["messages"]):
            if isinstance(m, ToolMessage):
                results.append(m)
            elif isinstance(m, AIMessage):
                break
            else:
                return None  # a new user message: a fresh turn
        done = results and all(r.name in self._tools and direct_reply_of(r.artifact) for r in results)
        return {"jump_to": "end"} if done else None
