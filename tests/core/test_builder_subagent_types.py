"""Builder wiring for registry-driven subagent types."""

import json
from pathlib import Path

import pytest

from cluxmate.core.builder import AgentBuilder
from cluxmate.tools.task import TaskTool


class _Provider:
    def __init__(self, model="parent-model"):
        self.model = model


def _home(monkeypatch, tmp_path) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def _write_agent(tmp_path: Path, slug: str, body: str) -> None:
    d = tmp_path / ".cluxmate" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{slug}.md").write_text(body, encoding="utf-8")


def _write_config(home: Path, model_id: str) -> None:
    (home / ".cluxmate").mkdir(parents=True, exist_ok=True)
    (home / ".cluxmate" / "config.json").write_text(json.dumps({
        "version": 2,
        "models": [{
            "id": model_id, "api_type": "openai", "provider": "deepseek",
            "base_url": "https://example.invalid", "api_key": "k",
            "model_name": "small-model", "context_1m": True, "max_tokens": 1024,
        }],
        "active_model_id": model_id,
    }), encoding="utf-8")


def test_custom_type_toolset_is_filtered(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    _write_agent(tmp_path, "reviewer", (
        "---\ndescription: review\ntools: [read_file, grep]\n---\nYou review code.\n"
    ))
    b = AgentBuilder(str(tmp_path), _Provider()).with_default_tools().with_subagents()
    child = b.build_child("reviewer", "review this", "child-1")
    names = {d["name"] for d in child.tools.definitions()}
    assert names == {"read_file", "grep"}
    assert "You review code." in child.system_prompt


def test_custom_type_gets_no_task_tool_by_default(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    _write_agent(tmp_path, "writer", "---\ndescription: w\ntools: [write_file]\n---\n")
    b = AgentBuilder(str(tmp_path), _Provider()).with_default_tools().with_subagents()
    assert b.build_child("writer", "d", "c1").tools.tool("task") is None
    # explicit opt-in works: task in the tool list AND subagents rights
    _write_agent(tmp_path, "boss", (
        "---\ndescription: b\ntools: [read_file, task]\nsubagents: inherit\n---\n"
    ))
    b2 = AgentBuilder(str(tmp_path), _Provider()).with_default_tools().with_subagents()
    assert b2.build_child("boss", "d", "c2").tools.tool("task") is not None


def test_explore_child_can_only_spawn_explore(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    b = AgentBuilder(str(tmp_path), _Provider()).with_default_tools().with_subagents()
    child = b.build_child("explore", "d", "c1")
    task_tool = child.tools.tool("task")
    assert isinstance(task_tool, TaskTool)
    # the grandchild builder may only offer explore
    assert task_tool.input_schema["properties"]["subagent_type"]["enum"] == ["explore"]


def test_model_override_swaps_provider_and_window(tmp_path, monkeypatch):
    home = _home(monkeypatch, tmp_path)
    _write_config(home, "fast")
    _write_agent(tmp_path, "quick", "---\ndescription: q\nmodel: fast\n---\n")
    parent = _Provider()
    b = AgentBuilder(str(tmp_path), parent).with_default_tools().with_subagents()
    child = b.build_child("quick", "d", "c1")
    assert child.provider is not parent
    assert child.model == "small-model"
    assert child.context_window == 1_000_000


def test_task_tool_lists_registry_types(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    _write_agent(tmp_path, "reviewer", "---\ndescription: review\n---\n")
    b = AgentBuilder(str(tmp_path), _Provider()).with_default_tools().with_subagents()
    tool = next(t for t in b._get_tools() if t.name == "task")
    enum = tool.input_schema["properties"]["subagent_type"]["enum"]
    assert enum == ["general-purpose", "explore", "reviewer"]
    # (the per-type catalog text lands on the schema in Task 3)


def test_builtin_behavior_unchanged(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    b = AgentBuilder(str(tmp_path), _Provider()).with_default_tools().with_subagents()
    gp = b.build_child("general-purpose", "d", "c1")
    assert {"bash", "write_file", "task"} <= {d["name"] for d in gp.tools.definitions()}
    assert gp.provider is b._provider


def test_no_subagents_flag_means_no_task_tool(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    b = AgentBuilder(str(tmp_path), _Provider()).with_default_tools()
    assert "task" not in {t.name for t in b._get_tools()}


@pytest.mark.asyncio
async def test_scheduler_is_shared_with_children(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    b = AgentBuilder(str(tmp_path), _Provider()).with_default_tools().with_subagents()
    s = b._scheduler_for_loop()
    child_builder = b._child_builder(b.agent_type("explore"), "c1")
    assert child_builder._scheduler is s
