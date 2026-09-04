"""Tests for builder wiring of TodoTool and the task_tracking prompt block."""

from cluxmate.core.builder import AgentBuilder
from cluxmate.tools.todo import TodoTool


class _Provider:
    pass


def _builder(tmp_path, mode="default"):
    b = AgentBuilder(str(tmp_path), _Provider())
    b.with_default_tools().with_mode(mode)
    return b


def _names(tools):
    return [t.name for t in tools]


def test_default_mode_registers_todo_write(tmp_path):
    tools = _builder(tmp_path)._get_tools()
    assert "todo_write" in _names(tools)
    assert isinstance(next(t for t in tools if t.name == "todo_write"), TodoTool)


def test_plan_mode_registers_todo_write(tmp_path):
    # todo_write is read-only in the sandbox sense (it only appends a
    # session-log event), so plan mode's hard isolation still includes it.
    tools = _builder(tmp_path, mode="plan")._get_tools()
    assert "todo_write" in _names(tools)


def test_child_builder_has_no_todo_write(tmp_path):
    # The task list belongs to the root session the user watches; subagents
    # never register the tool (mirrors update_memory/skill gating).
    b = _builder(tmp_path)
    child = b._child_builder("general-purpose", "c1")
    assert "todo_write" not in _names(child._get_tools())


def test_system_prompt_mentions_task_tracking_when_tool_present(tmp_path):
    b = _builder(tmp_path)
    prompt = b._render_system_prompt(b._get_tools())
    assert "<task_tracking>" in prompt
    assert "`todo_write`" in prompt
    # The claim-vs-evidence rule: the completion protocol applies to the list,
    # not just the final reply (prompt-level guard against fake completion).
    assert "the same claim-vs-tool reconciliation applies to the list" in prompt


def test_child_prompt_omits_task_tracking(tmp_path):
    # Children get no todo tool, so their prompt must not advertise the rules.
    b = _builder(tmp_path)
    child = b._child_builder("general-purpose", "c1")
    prompt = child._render_system_prompt(child._get_tools())
    assert "<task_tracking>" not in prompt
