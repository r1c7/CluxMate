"""Tests for the sandbox escalation mechanism (sandbox_permissions).

Covers the vocabulary/validation, the fence's escalate mode (skip containment
but keep the deny subtree), and the file tools' one-shot bypass.
"""

import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest

from cluxmate.core.agent import AgentCallbacks, AgentLoop
from cluxmate.core.providers.base import LLMResponse, ToolCall
from cluxmate.tools.base import BaseTool, ToolBridge
from cluxmate.tools._sandbox import (
    ESCALATION_HINT,
    ESCALATION_SCHEMA_FIELDS,
    validate_escalation_args,
)
from cluxmate.tools._fence import SandboxViolation, WriteFence
from cluxmate.tools.delete_file import DeleteFileTool
from cluxmate.tools.write_file import WriteFileTool


# ---------------------------------------------------------------------------
# Vocabulary + validation
# ---------------------------------------------------------------------------

def test_escalation_schema_fields_shape():
    assert set(ESCALATION_SCHEMA_FIELDS) == {"sandbox_permissions", "justification"}
    assert ESCALATION_SCHEMA_FIELDS["sandbox_permissions"]["enum"] == ["danger-full-access"]


def test_validate_no_request_is_ok():
    assert validate_escalation_args(None, None) is None


def test_validate_requires_pairing():
    assert "together" in validate_escalation_args("danger-full-access", None)
    assert "together" in validate_escalation_args(None, "because")


def test_validate_rejects_bad_mode():
    assert "danger-full-access" in validate_escalation_args("read-only", "because")


def test_validate_rejects_empty_justification():
    assert "non-empty" in validate_escalation_args("danger-full-access", "   ")


def test_validate_accepts_valid_pair():
    assert validate_escalation_args("danger-full-access", "needs wider access") is None


# ---------------------------------------------------------------------------
# Fence escalate mode
# ---------------------------------------------------------------------------

def test_fence_escalate_skips_containment(tmp_path):
    # Without escalate, home is denied; with escalate it passes (containment
    # skipped) but the deny subtree still applies.
    fence = WriteFence(str(tmp_path))
    home_file = Path.home() / "cluxmate-fence-esc.txt"
    with pytest.raises(SandboxViolation):
        fence.check(home_file)
    assert fence.check(home_file, escalate=True) == home_file.resolve()


def test_fence_escalate_still_denies_state_dir(tmp_path):
    fence = WriteFence(str(tmp_path))
    with pytest.raises(SandboxViolation):
        fence.check(tmp_path / ".cluxmate" / "permissions.json", escalate=True)


def test_fence_denial_carries_escalation_hint(tmp_path):
    fence = WriteFence(str(tmp_path))
    msg = fence.check_message(Path.home() / "cluxmate-x.txt")
    assert ESCALATION_HINT in msg


def test_fence_deny_subtree_has_no_hint(tmp_path):
    # deny-subtree rejections must NOT carry the escalation hint (escalation
    # doesn't open them).
    fence = WriteFence(str(tmp_path))
    msg = fence.check_message(tmp_path / ".cluxmate" / "x")
    assert ESCALATION_HINT not in msg


# ---------------------------------------------------------------------------
# File tools: one-shot bypass via sandbox_permissions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_write_file_escalate_writes_outside_workspace(tmp_path):
    target = Path.home() / "cluxmate-esc-write-test.txt"
    assert not target.exists()
    try:
        tool = WriteFileTool(workdir=str(tmp_path))
        result = await tool.execute(
            path=str(target), content="esc", 
            sandbox_permissions="danger-full-access", justification="test",
        )
        assert "Created" in result
        assert target.read_text(encoding="utf-8") == "esc"
    finally:
        target.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_delete_file_escalate_deletes_outside_workspace(tmp_path):
    target = Path.home() / "cluxmate-esc-delete-test.txt"
    target.write_text("bye", encoding="utf-8")
    try:
        tool = DeleteFileTool(workdir=str(tmp_path))
        result = await tool.execute(
            path=str(target),
            sandbox_permissions="danger-full-access", justification="test",
        )
        assert "Deleted" in result
        assert not target.exists()
    finally:
        target.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_write_file_escalate_still_blocked_by_deny_subtree(tmp_path):
    tool = WriteFileTool(workdir=str(tmp_path))
    result = await tool.execute(
        path=str(tmp_path / ".cluxmate" / "permissions.json"), content="x",
        sandbox_permissions="danger-full-access", justification="test",
    )
    assert "protected directory" in result
    assert not (tmp_path / ".cluxmate" / "permissions.json").exists()


# ---------------------------------------------------------------------------
# Agent loop: escalation raises risk to dangerous (drives the approval prompt)
# ---------------------------------------------------------------------------

class _AnyTool(BaseTool):
    """Accepts any kwargs so escalation args don't break the call."""

    @property
    def name(self): return "probe"

    @property
    def description(self): return "probe"

    @property
    def input_schema(self): return {"type": "object", "properties": {}}

    async def execute(self, **kwargs): return "ok"


class _RiskRecorder(AgentCallbacks):
    def __init__(self):
        self.starts = []

    async def on_tool_start(self, name, params, call_id, risk_level):
        self.starts.append((name, risk_level))
        return True


class _FakeProvider:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def chat(self, messages, tools, *, on_delta=None, on_thinking=None):
        self.calls.append(messages)
        return self.responses.pop(0) if self.responses else LLMResponse(text="done", stop_reason="end_turn")

    def assistant_message_to_api(self, msg): return {"role": "assistant", "content": msg.text or ""}
    def tool_result_to_api(self, result): return {"role": "user", "content": result.content}
    def max_tokens(self): return 1000


@pytest.mark.asyncio
async def test_escalation_raises_risk_to_dangerous():
    bridge = ToolBridge()
    bridge.register(_AnyTool())
    provider = _FakeProvider([
        LLMResponse(tool_calls=[ToolCall(
            id="t1", name="probe",
            input={"sandbox_permissions": "danger-full-access", "justification": "needs wider access"},
        )], stop_reason="tool_use"),
        LLMResponse(text="done", stop_reason="end_turn"),
    ])
    agent = AgentLoop(model="test", provider=provider, tools=bridge, system_prompt="Test")
    cbs = _RiskRecorder()
    await agent.run("go", callbacks=cbs)
    assert cbs.starts == [("probe", "dangerous")]


@pytest.mark.asyncio
async def test_escalation_malformed_rejected_without_approval():
    bridge = ToolBridge()
    bridge.register(_AnyTool())
    provider = _FakeProvider([
        LLMResponse(tool_calls=[ToolCall(
            id="t1", name="probe",
            input={"sandbox_permissions": "danger-full-access"},  # missing justification
        )], stop_reason="tool_use"),
        LLMResponse(text="done", stop_reason="end_turn"),
    ])
    agent = AgentLoop(model="test", provider=provider, tools=bridge, system_prompt="Test")
    cbs = _RiskRecorder()
    await agent.run("go", callbacks=cbs)
    # No approval prompt fired (rejected as malformed), and the error was fed
    # back to the model.
    assert cbs.starts == []
    second_call = provider.calls[1]
    joined = "\n".join(str(m) for m in second_call)
    assert "together" in joined


@pytest.mark.asyncio
async def test_escalation_bad_mode_rejected():
    bridge = ToolBridge()
    bridge.register(_AnyTool())
    provider = _FakeProvider([
        LLMResponse(tool_calls=[ToolCall(
            id="t1", name="probe",
            input={"sandbox_permissions": "read-only", "justification": "because"},
        )], stop_reason="tool_use"),
        LLMResponse(text="done", stop_reason="end_turn"),
    ])
    agent = AgentLoop(model="test", provider=provider, tools=bridge, system_prompt="Test")
    cbs = _RiskRecorder()
    await agent.run("go", callbacks=cbs)
    assert cbs.starts == []
    joined = "\n".join(str(m) for m in provider.calls[1])
    assert "danger-full-access" in joined


# ---------------------------------------------------------------------------
# Bash: escalation runs unsandboxed (bare)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(Path("C:/").exists() is False, reason="windows-only")
@pytest.mark.asyncio
async def test_bash_escalate_runs_unsandboxed():
    from cluxmate.tools.bash import BashTool
    from cluxmate.tools._sandbox import WindowsLowILSandbox
    ws = Path(tempfile.mkdtemp(prefix="cluxmate-esc-bash-"))
    target = Path.home() / "cluxmate-bash-esc-test.txt"
    assert not target.exists()
    try:
        tool = BashTool(
            workdir=str(ws),
            sandbox=WindowsLowILSandbox(str(ws)),
            sandbox_required=True,
        )
        result = await tool.execute(
            command=f"echo esc > {target}",
            sandbox_permissions="danger-full-access",
            justification="test",
        )
        assert "exit code" not in result
        assert target.exists()
    finally:
        target.unlink(missing_ok=True)
        shutil.rmtree(ws, ignore_errors=True)