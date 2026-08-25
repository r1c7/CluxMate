"""Tests for lifecycle hooks (HookManager + AgentLoop integration)."""

import json
import sys
from pathlib import Path

import pytest

from cluxmate.core.agent import AgentLoop
from cluxmate.core.hooks import HookManager
from cluxmate.core.providers.base import LLMResponse, ToolCall
from cluxmate.tools.base import BaseTool, ToolBridge

# A tiny helper script the hook commands point at. Each mode exercises one
# branch of the stdout/exit-code contract.
_HELPER = '''import json, sys
mode = sys.argv[1] if len(sys.argv) > 1 else ""
if mode == "exit2":
    sys.exit(2)
data = json.load(sys.stdin)
if mode == "block":
    print(json.dumps({"decision": "block", "reason": "hook says no"}))
elif mode == "block_continue_false":
    print(json.dumps({"continue": False, "reason": "nope"}))
elif mode == "feedback":
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": data.get("hook_event_name"),
        "additionalContext": "HELLO_FROM_HOOK",
    }}))
elif mode == "nonjson":
    print("plain text output")
elif mode == "sleep":
    import time
    time.sleep(10)
elif mode == "echo":
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "x", "additionalContext": json.dumps(data),
    }}))
'''


def _make_mgr(tmp_path, monkeypatch, event, mode, matcher=None, timeout=None):
    """A HookManager whose global root is tmp_path/home and cwd is tmp_path/proj,
    with one hook for ``event`` pointing at the helper script in ``mode``."""
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    proj = tmp_path / "proj"
    proj.mkdir()
    script = proj / "hook_helper.py"
    script.write_text(_HELPER, encoding="utf-8")
    cmd = f'"{sys.executable}" {script.name} {mode}'
    entry = {"hooks": [{"type": "command", "command": cmd}]}
    if matcher is not None:
        entry["matcher"] = matcher
    if timeout is not None:
        entry["hooks"][0]["timeout"] = timeout
    (proj / ".cluxmate").mkdir(parents=True, exist_ok=True)
    (proj / ".cluxmate" / "settings.json").write_text(
        json.dumps({"hooks": {event: [entry]}}), encoding="utf-8"
    )
    return HookManager(str(proj))


# ── config loading & matching ───────────────────────────────────────────────

def test_no_settings_yields_no_events(tmp_path, monkeypatch):
    mgr = _make_mgr(tmp_path, monkeypatch, "PreToolUse", "block")
    assert mgr.has_event("PreToolUse") is True
    assert mgr.has_event("PostToolUse") is False


def test_matcher_filters_by_tool_name(tmp_path, monkeypatch):
    import asyncio
    mgr = _make_mgr(tmp_path, monkeypatch, "PreToolUse", "block", matcher="bash")
    # Matches: the hook runs and blocks.
    result = asyncio.run(mgr.run_event("PreToolUse", tool_name="bash"))
    assert result.blocked is True
    # No match: the hook does not run (and cannot block).
    result = asyncio.run(mgr.run_event("PreToolUse", tool_name="grep"))
    assert result.blocked is False


def test_matcher_none_matches_everything(tmp_path, monkeypatch):
    import asyncio
    mgr = _make_mgr(tmp_path, monkeypatch, "Stop", "block")
    result = asyncio.run(mgr.run_event("Stop"))
    assert result.blocked is True


def test_global_and_project_both_load(tmp_path, monkeypatch):
    """Global and project hooks for the same event both run (global first)."""
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    proj = tmp_path / "proj"
    proj.mkdir()
    script = proj / "hook_helper.py"
    script.write_text(_HELPER, encoding="utf-8")

    def write_settings(path: Path, mode: str):
        path.mkdir(parents=True, exist_ok=True)
        cmd = f'"{sys.executable}" {script.name} {mode}'
        (path / "settings.json").write_text(json.dumps({
            "hooks": {"PostToolUse": [
                {"hooks": [{"type": "command", "command": cmd}]},
            ]},
        }), encoding="utf-8")

    write_settings(home / ".cluxmate", "feedback")
    write_settings(proj / ".cluxmate", "feedback")

    mgr = HookManager(str(proj))
    import asyncio
    result = asyncio.run(mgr.run_event("PostToolUse", tool_name="bash"))
    assert result.feedback == ["HELLO_FROM_HOOK", "HELLO_FROM_HOOK"]


# ── output contract ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_block_via_decision(tmp_path, monkeypatch):
    mgr = _make_mgr(tmp_path, monkeypatch, "PreToolUse", "block")
    result = await mgr.run_event("PreToolUse", tool_name="bash")
    assert result.blocked is True
    assert result.reason == "hook says no"


@pytest.mark.asyncio
async def test_block_via_continue_false(tmp_path, monkeypatch):
    mgr = _make_mgr(tmp_path, monkeypatch, "PreToolUse", "block_continue_false")
    result = await mgr.run_event("PreToolUse", tool_name="bash")
    assert result.blocked is True
    assert result.reason == "nope"


@pytest.mark.asyncio
async def test_block_via_exit_code_2(tmp_path, monkeypatch):
    mgr = _make_mgr(tmp_path, monkeypatch, "PreToolUse", "exit2")
    result = await mgr.run_event("PreToolUse", tool_name="bash")
    assert result.blocked is True


@pytest.mark.asyncio
async def test_feedback_collected(tmp_path, monkeypatch):
    mgr = _make_mgr(tmp_path, monkeypatch, "PostToolUse", "feedback")
    result = await mgr.run_event("PostToolUse", tool_name="bash")
    assert result.blocked is False
    assert result.feedback == ["HELLO_FROM_HOOK"]


@pytest.mark.asyncio
async def test_nonjson_stdout_is_noop(tmp_path, monkeypatch):
    mgr = _make_mgr(tmp_path, monkeypatch, "PostToolUse", "nonjson")
    result = await mgr.run_event("PostToolUse", tool_name="bash")
    assert result.blocked is False
    assert result.feedback == []


@pytest.mark.asyncio
async def test_unknown_command_is_noop(tmp_path, monkeypatch):
    """A hook pointing at a nonexistent command degrades to a no-op, never a raise."""
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".cluxmate").mkdir()
    (proj / ".cluxmate" / "settings.json").write_text(json.dumps({
        "hooks": {"PreToolUse": [
            {"hooks": [{"type": "command", "command": "this_command_does_not_exist_12345"}]},
        ]},
    }), encoding="utf-8")
    mgr = HookManager(str(proj))
    result = await mgr.run_event("PreToolUse", tool_name="bash")
    assert result.blocked is False
    assert result.feedback == []


@pytest.mark.asyncio
async def test_timeout_is_noop(tmp_path, monkeypatch):
    mgr = _make_mgr(tmp_path, monkeypatch, "PreToolUse", "sleep", timeout=0.2)
    result = await mgr.run_event("PreToolUse", tool_name="bash")
    assert result.blocked is False
    assert result.feedback == []


@pytest.mark.asyncio
async def test_payload_carries_tool_context(tmp_path, monkeypatch):
    """The hook receives a JSON payload on stdin with the tool details."""
    mgr = _make_mgr(tmp_path, monkeypatch, "PreToolUse", "echo")
    result = await mgr.run_event(
        "PreToolUse", tool_name="bash", tool_input={"command": "ls"},
    )
    assert len(result.feedback) == 1
    payload = json.loads(result.feedback[0])
    assert payload["hook_event_name"] == "PreToolUse"
    assert payload["tool_name"] == "bash"
    assert payload["tool_input"] == {"command": "ls"}


# ── AgentLoop integration ───────────────────────────────────────────────────

class _FakeProvider:
    """Records calls; returns queued responses, then 'done'."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def chat(self, messages, tools, *, on_delta=None, on_thinking=None):
        self.calls.append((messages, tools))
        if self.responses:
            return self.responses.pop(0)
        return LLMResponse(text="done", stop_reason="end_turn")

    def assistant_message_to_api(self, msg):
        return {"role": "assistant", "content": msg.text or ""}

    def tool_result_to_api(self, result):
        return {"role": "user", "content": result.content}

    def max_tokens(self):
        return 1000


class _EchoTool(BaseTool):
    def __init__(self):
        self.executed = []

    @property
    def name(self):
        return "echo"

    @property
    def description(self):
        return "Echo."

    @property
    def input_schema(self):
        return {"type": "object", "properties": {"message": {"type": "string"}}}

    async def execute(self, message: str = ""):
        self.executed.append(message)
        return f"ECHO: {message}"


@pytest.mark.asyncio
async def test_user_prompt_submit_block_short_circuits(tmp_path, monkeypatch):
    """A blocking UserPromptSubmit hook rejects the prompt before the model runs."""
    mgr = _make_mgr(tmp_path, monkeypatch, "UserPromptSubmit", "block")
    provider = _FakeProvider([])
    agent = AgentLoop("t", provider, ToolBridge(), system_prompt="Test", hooks=mgr)

    result = await agent.run("hello")

    assert result.text == "hook says no"
    assert provider.calls == []  # the model was never called


@pytest.mark.asyncio
async def test_pretool_use_block_denies_tool(tmp_path, monkeypatch):
    """A blocking PreToolUse hook denies the tool and feeds the reason back."""
    mgr = _make_mgr(tmp_path, monkeypatch, "PreToolUse", "block", matcher="echo")
    bridge = ToolBridge()
    echo = _EchoTool()
    bridge.register(echo)
    provider = _FakeProvider([
        LLMResponse(
            tool_calls=[ToolCall(id="t1", name="echo", input={"message": "x"})],
            stop_reason="tool_use",
        ),
        LLMResponse(text="done", stop_reason="end_turn"),
    ])
    agent = AgentLoop("t", provider, bridge, system_prompt="Test", hooks=mgr)

    result = await agent.run("go")

    assert result.text == "done"
    assert echo.executed == []  # the tool never ran
    # The block reason reached the model as the tool result.
    followup = provider.calls[1][0]
    blocked = [
        m for m in followup
        if m.get("role") == "user" and "hook says no" in m.get("content", "")
    ]
    assert len(blocked) == 1


@pytest.mark.asyncio
async def test_posttool_use_feedback_injected(tmp_path, monkeypatch):
    """PostToolUse feedback is injected as a synthetic user message for the next request."""
    mgr = _make_mgr(tmp_path, monkeypatch, "PostToolUse", "feedback", matcher="echo")
    bridge = ToolBridge()
    bridge.register(_EchoTool())
    provider = _FakeProvider([
        LLMResponse(
            tool_calls=[ToolCall(id="t1", name="echo", input={"message": "x"})],
            stop_reason="tool_use",
        ),
        LLMResponse(text="done", stop_reason="end_turn"),
    ])
    agent = AgentLoop("t", provider, bridge, system_prompt="Test", hooks=mgr)

    result = await agent.run("go")

    assert result.text == "done"
    followup = provider.calls[1][0]
    injected = [
        m for m in followup
        if m.get("role") == "user" and "HELLO_FROM_HOOK" in m.get("content", "")
    ]
    assert len(injected) == 1


@pytest.mark.asyncio
async def test_stop_feedback_injected_into_history(tmp_path, monkeypatch):
    """Stop feedback becomes context for the NEXT turn (present in returned history)."""
    mgr = _make_mgr(tmp_path, monkeypatch, "Stop", "feedback")
    provider = _FakeProvider([LLMResponse(text="final", stop_reason="end_turn")])
    agent = AgentLoop("t", provider, ToolBridge(), system_prompt="Test", hooks=mgr)

    result = await agent.run("hello")

    assert result.text == "final"
    assert any(
        m.get("role") == "user" and "HELLO_FROM_HOOK" in m.get("content", "")
        for m in (result.history or [])
    )
