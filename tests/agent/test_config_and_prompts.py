"""Config files are the single source of tunables; prompts are versioned templates with strict variables."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from nwb.conf import (
    AgentConfig,
    ClassificationConfig,
    ConfigLoader,
    KnowledgeConfig,
    MemoryConfig,
    PersonasConfig,
    SourcesConfig,
)
from nwb.conf.models import ActionsConfig, HeartbeatConfig
from nwb.errors import ConfigError, PromptError
from nwb.prompts import PromptLibrary
from tests.conftest import ROOT


def test_all_shipped_configs_validate() -> None:
    loader = ConfigLoader(ROOT / "config")
    agent = loader.load("agent", AgentConfig)
    loader.load("memory", MemoryConfig)
    loader.load("classification", ClassificationConfig)
    loader.load("knowledge", KnowledgeConfig)
    loader.load("sources", SourcesConfig)
    loader.load("heartbeat", HeartbeatConfig)
    loader.load("actions", ActionsConfig)
    personas = ConfigLoader(ROOT / "demo").load("personas", PersonasConfig)
    assert "delete" in agent.harness.excluded_tools
    assert personas.get("NW1103") is not None


def test_delete_tool_cannot_be_re_enabled_by_config() -> None:
    raw = ConfigLoader(ROOT / "config").load("agent", AgentConfig).model_dump()
    raw["harness"]["excluded_tools"] = ["execute"]
    assert "delete" in AgentConfig.model_validate(raw).harness.excluded_tools


def test_permissions_must_be_deny_first() -> None:
    raw = ConfigLoader(ROOT / "config").load("agent", AgentConfig).model_dump()
    raw["permissions"] = [
        {"operations": ["read", "write"], "paths": ["/**"], "mode": "allow"},
        {"operations": ["write"], "paths": ["/skills/**"], "mode": "deny"},
    ]
    with pytest.raises(ValidationError, match="deny"):
        AgentConfig.model_validate(raw)


def test_unknown_config_keys_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "memory.yaml").write_text("content_max_chars: 10\ntypo_key: 1\n")
    with pytest.raises(ConfigError):
        ConfigLoader(tmp_path).load("memory", MemoryConfig)


def test_yaml_on_is_not_a_boolean_stopword() -> None:
    cfg = ConfigLoader(ROOT / "config").load("memory", MemoryConfig)
    assert "on" in cfg.search.stopwords


def test_prompts_render_with_version_ref_and_strict_inputs() -> None:
    lib = PromptLibrary(ROOT / "prompts")
    out = lib.render("system", company_name="Northwind", agent_name="nwb")
    assert out.ref.startswith("system@v")
    assert "Northwind" in out.text
    with pytest.raises(PromptError):
        lib.render("system", company_name="Northwind")  # agent_name missing -> fail loudly


def test_every_prompt_declares_its_inputs() -> None:
    lib = PromptLibrary(ROOT / "prompts")
    for path in sorted((ROOT / "prompts").glob("*.md")):
        tpl = lib.get(path.stem)
        assert tpl.ref.startswith(f"{path.stem}@v")
