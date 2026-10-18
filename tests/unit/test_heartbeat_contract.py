"""Heartbeat contract that does not need a database: silence tokens, active-hours schema, role files."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from nwb.conf.models import ActiveHours, HeartbeatConfig
from nwb.jobs.heartbeat import PROBES, deliverable_text
from tests.conftest import ROOT


def test_silence_tokens_come_from_config_and_are_not_delivered() -> None:
    shipped = HeartbeatConfig()
    assert deliverable_text("NO_REPLY", shipped) is None
    assert deliverable_text('  "NO_REPLY"  ', shipped) is None
    assert deliverable_text("HEARTBEAT_OK", shipped) is None
    assert deliverable_text("HEARTBEAT_OK\nAll quiet here.", shipped) is None

    long = "x" * (shipped.max_chars + 40)
    kept = deliverable_text(f"{long}\nNO_REPLY", shipped)
    assert kept is not None and "NO_REPLY" not in kept and len(kept) == shipped.max_chars

    custom = HeartbeatConfig(silence_token="QUIET", ack_token="DONE")
    assert deliverable_text("QUIET", custom) is None
    assert deliverable_text("DONE: short note", custom) is None
    assert deliverable_text("NO_REPLY", custom) == "NO_REPLY"


def test_active_hours_and_delivery_are_schema_not_a_hardcoded_window() -> None:
    with pytest.raises(ValidationError, match="later than start"):
        ActiveHours(start="19:00", end="09:00")
    with pytest.raises(ValidationError):
        ActiveHours(start="morning", end="evening")
    raw = HeartbeatConfig().model_dump()
    raw["delivery"] = {"to": "group", "channel": "inbox"}
    with pytest.raises(ValidationError):
        HeartbeatConfig.model_validate(raw)
    raw["delivery"] = {"to": "owner", "channel": "group"}
    with pytest.raises(ValidationError):
        HeartbeatConfig.model_validate(raw)
    assert set(HeartbeatConfig().wake_gate.checks[0].model_dump()) >= {"name", "within_days"}
    assert {check.name for check in HeartbeatConfig().wake_gate.checks} <= set(PROBES)


def _scheduled_roles(root: Path) -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    for path in sorted(root.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            continue
        meta = yaml.safe_load(text.split("---", 2)[1]) or {}
        md = meta.get("metadata") if isinstance(meta, dict) else None
        if isinstance(md, dict) and md.get("scheduled") is True:
            found.append((path, text.split("---", 2)[2]))
    return found


def test_scheduled_agent_roles_state_what_they_decide_and_escalate() -> None:
    roles = _scheduled_roles(ROOT / "agent")
    assert roles, "a scheduled agent needs a role file marked metadata.scheduled"
    for path, body in roles:
        assert "## Decide" in body and "## Escalate" in body, path
        decide = body.split("## Decide", 1)[1].split("##", 1)[0]
        escalate = body.split("## Escalate", 1)[1]
        assert "-" in decide and "-" in escalate
    assert any(path.parent.name == "heartbeat" for path, _ in roles)
