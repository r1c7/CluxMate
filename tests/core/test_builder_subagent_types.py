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
    # The project file overrides the built-in reviewer slug in place, so it
    # appears exactly once.
    assert enum == ["general-purpose", "explore", "reviewer"]
    # spec §2.7: the per-type catalog (one line per type: description, tools,
    # model) lands on the schema, and the tool-level description carries it.
    assert "review" in tool.description
    desc = tool.input_schema["properties"]["subagent_type"]["description"]
    assert "- reviewer: review (tools: " in desc and "model: inherit" in desc
    assert "- general-purpose: " in desc and "- explore: " in desc


def test_builtin_behavior_unchanged(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    b = AgentBuilder(str(tmp_path), _Provider()).with_default_tools().with_subagents()
    gp = b.build_child("general-purpose", "d", "c1")
    assert {"bash", "write_file", "task"} <= {d["name"] for d in gp.tools.definitions()}
    assert gp.provider is b._provider


def test_system_prompt_advertises_the_review_gate(tmp_path, monkeypatch):
    """The gate prose ships with the capability, and only with it: an agent that
    cannot spawn a reviewer is never told to dispatch one."""
    _home(monkeypatch, tmp_path)
    b = AgentBuilder(str(tmp_path), _Provider()).with_default_tools().with_subagents()
    assert "<review_gate>" in b._render_system_prompt(b._get_tools())

    no_subs = AgentBuilder(str(tmp_path), _Provider()).with_default_tools()
    assert "<review_gate>" not in no_subs._render_system_prompt(no_subs._get_tools())


def test_review_gate_orders_the_review_after_the_work(tmp_path, monkeypatch):
    """The gate must say WHEN to dispatch, not only what to hand over: siblings
    in one tool-call block run concurrently and admission follows the order in
    the block, so a reviewer listed beside (or ahead of) the implementers would
    judge a tree they have not touched, and one holding a narrower claim would
    be corrupted by a writer running next to it."""
    _home(monkeypatch, tmp_path)
    b = AgentBuilder(str(tmp_path), _Provider()).with_default_tools().with_subagents()
    prompt = b._render_system_prompt(b._get_tools())
    assert "A review is a step of its own, AFTER the work" in prompt
    assert "Dispatch the reviewer only once the batch it judges has finished" in prompt
    assert "Omit `write_paths` for a reviewer" in prompt
    # The admission rule must state its real exception
    # (SubagentScheduler._pump skips a still-blocked earlier waiter rather than
    # stopping at it), not the absolute "block order decides who starts first".
    assert "a call that is not yet admissible is skipped rather" in prompt
    assert "takes the whole workspace while they queue behind" in prompt


def test_review_gate_scopes_the_re_review(tmp_path, monkeypatch):
    """A re-review must not be a second full audit. Measured on one plan review
    (session 2c2811eccf26): five rounds of 392-603 s each, and the last one spent
    546 s judging a 43-line fix diff because the prompt still said the whole
    6 000-line document was what had to be judged. The gate therefore has to name
    the scope (previous findings + the fix diff), keep script-decidable checks
    out of the reviewer's hands, guarantee the commands it needs, and cap the
    loop instead of reviewing until the budget runs out."""
    _home(monkeypatch, tmp_path)
    b = AgentBuilder(str(tmp_path), _Provider()).with_default_tools().with_subagents()
    prompt = b._render_system_prompt(b._get_tools())
    assert "A re-review is SCOPED" in prompt
    assert "never a second full audit" in prompt
    assert "Before you re-review, fix EVERY finding of the round" in prompt
    assert "Spend no round on what a script decides" in prompt
    # Followable by the agent reading it: always-allow is a user action, and the
    # permission config sits in the deny subtree the agent cannot write.
    assert "ask the user to always-allow the command" in prompt
    assert "the `bash:python` category" in prompt
    assert "Two rounds, then stop" in prompt


def test_builtin_reviewer_child_prompt_carries_its_contract(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    b = AgentBuilder(str(tmp_path), _Provider()).with_default_tools().with_subagents()
    child = b.build_child("reviewer", "review this change", "c1")
    # The registry instructions land in <agent_instructions> …
    assert "spec-compliance reviewer" in child.system_prompt
    # … and the toolset really is verify-but-never-edit.
    names = {d["name"] for d in child.tools.definitions()}
    assert names == {"read_file", "grep", "list_dir", "lsp", "bash"}
    assert "task" not in names
    # Rule 1 must not tell a reviewer it may modify what it is reviewing: bash
    # is the evidence tool, not an editing licence.
    assert "You can modify files and run commands." not in child.system_prompt
    assert "You hold no file-editing tool" in child.system_prompt


def test_readonly_and_editing_child_prompts_are_unchanged(tmp_path, monkeypatch):
    """The three-way branch keeps the two older types saying exactly what they
    said before: a writer may modify, a true read-only type may not."""
    _home(monkeypatch, tmp_path)
    b = AgentBuilder(str(tmp_path), _Provider()).with_default_tools().with_subagents()
    gp = b.build_child("general-purpose", "d", "c1")
    assert "You can modify files and run commands." in gp.system_prompt
    ex = b.build_child("explore", "d", "c2")
    assert "You are a read-only agent" in ex.system_prompt


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
