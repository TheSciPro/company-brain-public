"""Subagent filesystem writes fail closed unless the markdown opts in.

Deep Agents attaches write_file and edit_file to every subagent. The loader's default is read-only:
the tool allowlist omits those tools, FilesystemPermission denies every write, and WritePolicyBackend
rejects the write even if something calls the backend directly. ``read_only: true`` is accepted and
redundant. ``writes:`` is a path allowlist. ``read_only: false`` with no allowlist inherits the parent.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

import pytest
from deepagents import FilesystemPermission, create_deep_agent
from deepagents.backends import FilesystemBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import ValidationError

from nwb.agent.backends import WritePolicyBackend
from nwb.agent.subagents import SubAgent, build_subagents, load_subagent_files, split_markdown
from nwb.conf import AgentConfig, ConfigLoader
from nwb.errors import ConfigError
from tests.agent.conftest import ASOF, Harness
from tests.conftest import ROOT
from tests.fakes import ScriptedChatModel, call

_PLACEHOLDER = "- (nothing yet: ask or observe, then write it here)"


def _parent_permissions() -> list[FilesystemPermission]:
    cfg = ConfigLoader(ROOT / "config").load("agent", AgentConfig)
    return [
        FilesystemPermission(operations=list(rule.operations), paths=list(rule.paths), mode=rule.mode)
        for rule in cfg.permissions
    ]


def _stub(name: str) -> BaseTool:
    def _run(**_: object) -> str:
        return "ok"

    return StructuredTool.from_function(_run, name=name, description=name)


def _tools_for(directory: Path) -> dict[str, BaseTool]:
    names = {tool for spec, _ in load_subagent_files(directory) for tool in spec.tools}
    return {name: _stub(name) for name in names}


def _model_for(model: ScriptedChatModel) -> Callable[[Literal["main", "background"]], BaseChatModel]:
    def model_for(_role: Literal["main", "background"]) -> BaseChatModel:
        return model

    return model_for


def _specs(
    directory: Path,
    root: Path,
    *,
    model: ScriptedChatModel | None = None,
) -> tuple[list[SubAgent], ScriptedChatModel]:
    chat = model or ScriptedChatModel()
    return build_subagents(
        directory,
        _tools_for(directory),
        _model_for(chat),
        backend=FilesystemBackend(root_dir=root, virtual_mode=True),
        parent_permissions=_parent_permissions(),
    ), chat


def _write_subagent(directory: Path, name: str, extra: str = "") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    directory.joinpath(f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: A subagent used in tests only.\n{extra}---\nDo the task.\n",
        encoding="utf-8",
    )


def _by_name(specs: list[SubAgent]) -> dict[str, SubAgent]:
    return {spec["name"]: spec for spec in specs}


def _filesystem(spec: SubAgent) -> FilesystemMiddleware:
    found = [m for m in spec["middleware"] if isinstance(m, FilesystemMiddleware)]
    assert len(found) == 1
    middleware = found[0]
    assert isinstance(middleware.backend, WritePolicyBackend)
    return middleware


def _fs_tool_names(spec: SubAgent) -> set[str]:
    return {tool.name for tool in _filesystem(spec).tools}


def test_shipped_frontmatter_omits_redundant_read_only() -> None:
    """The loader default is read-only, so the explicit flag is not required."""
    auditor, _ = split_markdown((ROOT / "agent/subagents/auditor.md").read_text(encoding="utf-8"))
    researcher, _ = split_markdown((ROOT / "agent/subagents/researcher.md").read_text(encoding="utf-8"))
    keeper, _ = split_markdown((ROOT / "agent/subagents/memory-keeper.md").read_text(encoding="utf-8"))
    assert "read_only" not in auditor and "writes" not in auditor
    assert "read_only" not in researcher and "writes" not in researcher
    assert "read_only" not in keeper
    assert keeper["writes"] == ["/memories/USER.md", "/memories/MEMORY.md", "/memories/facts/**"]


def test_researcher_and_auditor_cannot_write_memories(tmp_path: Path) -> None:
    specs, _ = _specs(ROOT / "agent" / "subagents", tmp_path)
    specs = _by_name(specs)
    for name in ("researcher", "auditor"):
        spec = specs[name]
        assert "write_file" not in _fs_tool_names(spec)
        assert "edit_file" not in _fs_tool_names(spec)
        assert "read_file" in _fs_tool_names(spec)
        rules = spec["permissions"]
        assert rules[-1].mode == "deny" and "/**" in rules[-1].paths
        backend = _filesystem(spec).backend
        assert backend.write("/memories/USER.md", "pwned").error
        assert "permission denied" in (backend.write("/memories/USER.md", "pwned").error or "")
        assert backend.edit("/memories/USER.md", "a", "b").error
        assert not (tmp_path / "memories" / "USER.md").exists()


def test_explicit_read_only_true_matches_the_default(tmp_path: Path) -> None:
    _write_subagent(tmp_path / "off", "looker")
    _write_subagent(tmp_path / "on", "looker", "read_only: true\n")
    omitted, _ = _specs(tmp_path / "off", tmp_path / "disk-off")
    explicit, _ = _specs(tmp_path / "on", tmp_path / "disk-on")
    assert _fs_tool_names(omitted[0]) == _fs_tool_names(explicit[0])
    assert explicit[0]["permissions"][-1].mode == "deny"


def test_writes_allowlist_is_enforced_by_the_backend(tmp_path: Path) -> None:
    disk = tmp_path / "disk"
    _write_subagent(
        tmp_path / "agents",
        "scribe",
        "writes: [/scratch/**, /skills/evil.md]\n",
    )
    spec, _ = _specs(tmp_path / "agents", disk)
    spec = spec[0]
    assert {"write_file", "edit_file", "read_file"} <= _fs_tool_names(spec)
    backend = _filesystem(spec).backend
    assert backend.write("/scratch/notes/a.txt", "kept").error is None
    assert (disk / "scratch" / "notes" / "a.txt").read_text(encoding="utf-8") == "kept"
    edited = backend.edit("/scratch/notes/a.txt", "kept", "edited")
    assert edited.error is None and (disk / "scratch" / "notes" / "a.txt").read_text(encoding="utf-8") == "edited"
    denied = backend.write("/memories/USER.md", "nope")
    assert denied.error and "permission denied" in denied.error
    assert not (disk / "memories").exists()
    # Parent config denies /skills/** writes, and that deny is copied ahead of the allowlist.
    skill = backend.write("/skills/evil.md", "nope")
    assert skill.error and "permission denied" in skill.error
    assert not (disk / "skills").exists()
    assert backend.write("/memories/../skills/evil.md", "nope").error
    assert backend.upload_files([("/memories/USER.md", b"nope")])[0].error == "permission_denied"


def test_read_only_false_does_not_widen_a_writes_allowlist(tmp_path: Path) -> None:
    disk = tmp_path / "disk"
    _write_subagent(tmp_path / "agents", "scribe", "read_only: false\nwrites: [/scratch/**]\n")
    built, _ = _specs(tmp_path / "agents", disk)
    backend = _filesystem(built[0]).backend
    assert {"write_file", "edit_file"} <= _fs_tool_names(built[0])
    assert backend.write("/scratch/a.txt", "yes").error is None
    denied = backend.write("/memories/USER.md", "no")
    assert denied.error and "permission denied" in denied.error
    assert not (disk / "memories").exists()


def test_memory_keeper_writes_profile_files_and_fact_files(tmp_path: Path) -> None:
    disk = tmp_path / "disk"
    built, _ = _specs(ROOT / "agent" / "subagents", disk)
    spec = _by_name(built)["memory-keeper"]
    backend = _filesystem(spec).backend
    assert backend.write("/memories/USER.md", "user").error is None
    assert backend.write("/memories/MEMORY.md", "mem").error is None
    assert backend.write("/memories/facts/note.md", "I work on the Payments team.").error is None
    secret = backend.write("/memories/SECRET.md", "nope")
    assert secret.error and "permission denied" in secret.error
    assert backend.edit("/memories/SECRET.md", "a", "b").error
    assert (disk / "memories" / "USER.md").read_text(encoding="utf-8") == "user"
    assert (disk / "memories" / "MEMORY.md").read_text(encoding="utf-8") == "mem"
    assert (disk / "memories" / "facts" / "note.md").read_text(encoding="utf-8") == "I work on the Payments team."
    assert not (disk / "memories" / "SECRET.md").exists()
    # Reads of a file the inner backend already holds still work.
    assert backend.read("/memories/USER.md").error is None


def test_read_only_false_inherits_parent_writes(tmp_path: Path) -> None:
    _write_subagent(tmp_path / "agents", "scribe", "read_only: false\n")
    disk = tmp_path / "disk"
    built, model = _specs(tmp_path / "agents", disk)
    spec = built[0]
    assert "permissions" not in spec
    assert not any(isinstance(m, FilesystemMiddleware) for m in spec["middleware"])
    agent = create_deep_agent(
        model=model,
        tools=[],
        system_prompt="test",
        subagents=[spec],
        backend=FilesystemBackend(root_dir=disk, virtual_mode=True),
        permissions=_parent_permissions(),
    )

    def subagent(_: list[BaseMessage]) -> AIMessage:
        assert {"write_file", "edit_file"} <= set(model.bound_tools)
        return call("write_file", {"file_path": "/scratch/ok.txt", "content": "yes"}, "c-ok")

    def after_ok(messages: list[BaseMessage]) -> AIMessage:
        last = messages[-1]
        assert isinstance(last, ToolMessage) and last.status != "error"
        return call("write_file", {"file_path": "/org/AGENTS.md", "content": "no"}, "c-org")

    def after_org(messages: list[BaseMessage]) -> AIMessage:
        last = messages[-1]
        assert isinstance(last, ToolMessage) and last.status == "error"
        assert "permission denied" in str(last.content).lower()
        return AIMessage(content="done")

    model.script = [
        call("task", {"description": "Write the note.", "subagent_type": "scribe"}),
        subagent,
        after_ok,
        after_org,
        AIMessage(content="ok"),
    ]
    agent.invoke({"messages": [HumanMessage(content="go")]})
    assert (disk / "scratch" / "ok.txt").read_text(encoding="utf-8") == "yes"
    assert not (disk / "org" / "AGENTS.md").exists()


def test_bad_writes_path_is_rejected(tmp_path: Path) -> None:
    _write_subagent(tmp_path, "scribe", "writes: [../secret]\n")
    with pytest.raises(ValidationError, match="writes"):
        load_subagent_files(tmp_path)


def test_shipped_subagents_follow_model_frontmatter(tmp_path: Path) -> None:
    main, background = ScriptedChatModel(), ScriptedChatModel()

    def model_for(role: Literal["main", "background"]) -> BaseChatModel:
        return main if role == "main" else background

    specs = _by_name(
        build_subagents(
            ROOT / "agent" / "subagents",
            _tools_for(ROOT / "agent" / "subagents"),
            model_for,
            backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
            parent_permissions=_parent_permissions(),
        )
    )
    assert specs["researcher"]["model"] is main
    assert specs["auditor"]["model"] is background
    assert specs["memory-keeper"]["model"] is background


def test_unknown_subagent_model_fails_loudly(tmp_path: Path) -> None:
    _write_subagent(tmp_path, "scribe", "model: giant\n")
    with pytest.raises(ConfigError, match=r"scribe\.md: unknown model 'giant'"):
        load_subagent_files(tmp_path)


def test_unknown_subagent_tool_is_still_rejected(tmp_path: Path) -> None:
    _write_subagent(tmp_path, "scribe", "tools: [missing_tool]\n")
    with pytest.raises(ConfigError, match="missing_tool"):
        build_subagents(
            tmp_path,
            {},
            _model_for(ScriptedChatModel()),
            backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
        )


def test_compiled_researcher_and_auditor_expose_no_write_tools(harness: Harness) -> None:
    seen: dict[str, set[str]] = {}

    def capture(name: str):
        def step(_: list[BaseMessage]) -> AIMessage:
            seen[name] = set(harness.model.bound_tools)
            return call("write_file", {"file_path": "/memories/USER.md", "content": "pwned"}, f"c-{name}")

        def after(messages: list[BaseMessage]) -> AIMessage:
            last = messages[-1]
            assert isinstance(last, ToolMessage) and last.status == "error"
            return AIMessage(content="could not write")

        return step, after

    runner = harness.c.runner
    session = runner.start_session("NW1103", ASOF)
    company = harness.c.settings.company_id
    before = harness.c.memory_files.read(company, "NW1103", "USER.md")
    for name, question in (("researcher", "research this"), ("auditor", "check this draft")):
        step, after = capture(name)
        harness.model.script = [
            call("task", {"description": "Look it up.", "subagent_type": name}),
            step,
            after,
            AIMessage(content="done"),
        ]
        runner.turn("NW1103", session, question)
        assert "write_file" not in seen[name]
        assert "edit_file" not in seen[name]
        assert "read_file" in seen[name]
    assert harness.c.memory_files.read(company, "NW1103", "USER.md") == before


def test_compiled_memory_keeper_writes_only_allowlisted_paths(harness: Harness) -> None:
    runner = harness.c.runner
    session = runner.start_session("NW1103", ASOF)
    company = harness.c.settings.company_id
    user = harness.c.memory_files.read(company, "NW1103", "USER.md") or ""
    assert _PLACEHOLDER in user

    def subagent(_: list[BaseMessage]) -> AIMessage:
        bound = set(harness.model.bound_tools)
        assert {"write_file", "edit_file"} <= bound
        return call("write_file", {"file_path": "/memories/SECRET.md", "content": "nope"}, "c-secret")

    def after_secret(messages: list[BaseMessage]) -> AIMessage:
        last = messages[-1]
        assert isinstance(last, ToolMessage) and last.status == "error"
        assert "permission denied" in str(last.content).lower()
        return call("write_file", {"file_path": "/skills/evil/SKILL.md", "content": "nope"}, "c-skill")

    def after_skill(messages: list[BaseMessage]) -> AIMessage:
        last = messages[-1]
        assert isinstance(last, ToolMessage) and last.status == "error"
        assert "permission denied" in str(last.content).lower()
        return call(
            "edit_file",
            {"file_path": "/memories/USER.md", "old_string": _PLACEHOLDER, "new_string": "- Amounts in INR."},
            "c-edit",
        )

    def after_edit(messages: list[BaseMessage]) -> AIMessage:
        last = messages[-1]
        assert isinstance(last, ToolMessage) and last.status != "error"
        return AIMessage(content="updated USER.md")

    harness.model.script = [
        call("task", {"description": "Update memory.", "subagent_type": "memory-keeper"}),
        subagent,
        after_secret,
        after_skill,
        after_edit,
        AIMessage(content="Saved."),
    ]
    runner.turn("NW1103", session, "remember amounts in INR")
    assert "Amounts in INR." in (harness.c.memory_files.read(company, "NW1103", "USER.md") or "")
    assert harness.c.memory_files.read(company, "NW1103", "SECRET.md") is None
    assert not (harness.c.settings.agent_dir / "skills" / "evil").exists()
