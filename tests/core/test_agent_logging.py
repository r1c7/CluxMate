"""Tests for AgentLoop session-log instrumentation (Phase 3)."""

import json
from typing import Any

import pytest

from cluxmate.core.agent import AgentLoop
from cluxmate.core.builder import AgentBuilder
from cluxmate.core.providers.base import LLMResponse, ToolCall
from cluxmate.core.session_log import (
    APPEND,
    ReplaceOp,
    SessionHeader,
    SessionLog,
    fold_request_header,
)
from cluxmate.tools.base import BaseTool, ToolBridge


class RecordingProvider:
    """Fake provider that records calls and optionally streams text/reasoning."""

    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)
        self.calls: list[tuple[list[dict], list[dict]]] = []

    async def chat(self, messages, tools, *, on_delta=None, on_thinking=None):
        self.calls.append((messages, tools))
        resp = self.responses.pop(0) if self.responses else LLMResponse(text="done", stop_reason="end_turn")
        if on_thinking and resp.reasoning:
            await on_thinking(resp.reasoning)
        if on_delta and resp.text:
            await on_delta(resp.text)
        return resp

    def assistant_message_to_api(self, msg) -> dict:
        d: dict[str, Any] = {"role": "assistant", "content": msg.text or ""}
        if msg.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.input)},
                }
                for tc in msg.tool_calls
            ]
        return d

    def tool_result_to_api(self, result) -> dict:
        return {"role": "tool", "tool_call_id": result.tool_call_id, "content": result.content}

    def max_tokens(self) -> int:
        return 1000


class CompactingProvider(RecordingProvider):
    """RecordingProvider that answers the compaction summarizer separately.

    ``compact()`` summarizes the middle region with an empty tool list. Answer
    that call with a deterministic summary without recording it or consuming a
    queued response, so ``calls``/``responses`` reflect only the main loop.
    """

    async def chat(self, messages, tools, *, on_delta=None, on_thinking=None):
        if not tools:
            return LLMResponse(text="MIDDLE-SUMMARY", stop_reason="end_turn")
        return await super().chat(messages, tools, on_delta=on_delta, on_thinking=on_thinking)


class EchoTool(BaseTool):
    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Echo."

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {"message": {"type": "string"}}}

    async def execute(self, message: str = "") -> str:
        return f"ECHO: {message}"


def make_log(sid="s1") -> SessionLog:
    return SessionLog.create(SessionHeader(id=sid, createdAt=0, apiType="openai"))


def _types(log: SessionLog) -> list[str]:
    return [e.type for e in log.events]


@pytest.mark.asyncio
async def test_simple_text_emits_core_events():
    log = make_log()
    agent = AgentLoop(
        model="test",
        provider=RecordingProvider([LLMResponse(text="Hello", stop_reason="end_turn")]),
        tools=ToolBridge(),
        system_prompt="You are a test agent.",
        session_log=log,
    )
    result = await agent.run("Hi")
    assert result.text == "Hello"
    assert _types(log) == [
        "turn/start", "user/message", "step/start", "request/header",
        "assistant/message", "step/end", "turn/end",
    ]
    assert log.turn_count == 1
    # user/message carries source "human"
    user_evt = log.events[1]
    assert user_evt.data["source"] == "human"
    assert user_evt.data["message"] == {"role": "user", "content": "Hi"}
    # assistant/message projects to derive_messages
    assert log.derive_messages() == [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello"},
    ]
    # turn/end reason completed
    assert log.events[-1].data["reason"] == {"kind": "completed"}


@pytest.mark.asyncio
async def test_request_header_records_envelope():
    log = make_log()
    agent = AgentLoop(
        model="test",
        provider=RecordingProvider([LLMResponse(text="x", stop_reason="end_turn")]),
        tools=ToolBridge(),
        system_prompt="STABLE-PROMPT",
        session_log=log,
    )
    await agent.run("Hi")
    header_evt = next(e for e in log.events if e.type == "request/header")
    assert header_evt.data["reason"] == "initial"
    h = header_evt.data["header"]
    assert h["system"] == "STABLE-PROMPT"
    assert h["tools"] == []
    assert h["config"]["model"] == "test"
    assert h["config"]["max_tokens"] == 1000
    assert h["config"]["context_window"] == 128_000


@pytest.mark.asyncio
async def test_request_header_change_when_system_prompt_changes():
    log = make_log()
    agent = AgentLoop(
        model="test",
        provider=RecordingProvider([
            LLMResponse(text="a", stop_reason="end_turn"),
            LLMResponse(text="b", stop_reason="end_turn"),
        ]),
        tools=ToolBridge(),
        system_prompt="v1",
        session_log=log,
    )
    await agent.run("Hi")
    agent.system_prompt = "v2"
    await agent.run("Hi again")
    headers = [e.data for e in log.events if e.type == "request/header"]
    assert [h["reason"] for h in headers] == ["initial", "change"]
    assert headers[1]["header"]["system"] == "v2"


@pytest.mark.asyncio
async def test_tool_use_emits_tool_call_and_result():
    log = make_log()
    bridge = ToolBridge()
    bridge.register(EchoTool())
    agent = AgentLoop(
        model="test",
        provider=RecordingProvider([
            LLMResponse(tool_calls=[ToolCall(id="c1", name="echo", input={"message": "x"})], stop_reason="tool_use"),
            LLMResponse(text="done", stop_reason="end_turn"),
        ]),
        tools=bridge,
        system_prompt="s",
        session_log=log,
    )
    result = await agent.run("echo it")
    assert result.text == "done"
    call_evt = next(e for e in log.events if e.type == "tool/call")
    assert call_evt.data["callId"] == "c1"
    assert call_evt.data["name"] == "echo"
    assert call_evt.data["input"] == {"message": "x"}
    result_evt = next(e for e in log.events if e.type == "tool/result")
    assert result_evt.data["callId"] == "c1"
    assert result_evt.data["message"] == {"role": "tool", "tool_call_id": "c1", "content": "ECHO: x"}
    # balanced: step/start ... step/end for both steps, turn/end once
    assert _types(log).count("step/start") == 2
    assert _types(log).count("step/end") == 2


@pytest.mark.asyncio
async def test_injections_recorded_as_user_messages():
    log = make_log()
    agent = AgentLoop(
        model="test",
        provider=RecordingProvider([LLMResponse(text="x", stop_reason="end_turn")]),
        tools=ToolBridge(),
        system_prompt="s",
        session_log=log,
    )
    await agent.run("Hi", injections=[
        ("memory", "[Project memory]\nconventions"),
        ("skill", "[Available skills]\n- foo"),
    ])
    sources = [e.data["source"] for e in log.events if e.type == "user/message"]
    assert sources == ["memory", "skill", "human"]
    assert log.derive_messages()[0] == {"role": "user", "content": "[Project memory]\nconventions"}


@pytest.mark.asyncio
async def test_reasoning_accumulated_and_logged():
    log = make_log()
    agent = AgentLoop(
        model="test",
        provider=RecordingProvider([LLMResponse(text="answer", stop_reason="end_turn", reasoning="thinking...")]),
        tools=ToolBridge(),
        system_prompt="s",
        session_log=log,
    )
    from cluxmate.core.agent import AgentCallbacks
    await agent.run("Hi", callbacks=AgentCallbacks())
    assistant = next(e for e in log.events if e.type == "assistant/message")
    assert assistant.data["reasoning"] == "thinking..."
    # reasoning is kept separate from the message content
    assert assistant.data["message"]["content"] == "answer"


@pytest.mark.asyncio
async def test_no_session_log_keeps_legacy_behavior():
    """session_log=None must not emit events or change the result."""
    agent = AgentLoop(
        model="test",
        provider=RecordingProvider([LLMResponse(text="Hello", stop_reason="end_turn")]),
        tools=ToolBridge(),
        system_prompt="s",
        session_log=None,
    )
    result = await agent.run("Hi")
    assert result.text == "Hello"
    # history = messages[1:] (excludes system), unchanged legacy contract.
    assert result.history == [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello"},
    ]


@pytest.mark.asyncio
async def test_mode_recorded_in_request_header():
    log = make_log()
    agent = AgentLoop(
        model="test",
        provider=RecordingProvider([LLMResponse(text="x", stop_reason="end_turn")]),
        tools=ToolBridge(),
        system_prompt="s",
        session_log=log,
        mode="plan",
    )
    await agent.run("Hi")
    header = next(e for e in log.events if e.type == "request/header")
    assert header.data["header"]["config"]["mode"] == "plan"


@pytest.mark.asyncio
async def test_mode_change_emits_change_header():
    log = make_log()
    agent = AgentLoop(
        model="test",
        provider=RecordingProvider([
            LLMResponse(text="a", stop_reason="end_turn"),
            LLMResponse(text="b", stop_reason="end_turn"),
        ]),
        tools=ToolBridge(),
        system_prompt="s",
        session_log=log,
        mode="default",
    )
    await agent.run("Hi")
    # Simulate a mid-session mode switch: mode + system prompt change together.
    agent.mode = "plan"
    agent.system_prompt = "s (plan)"
    await agent.run("Hi again")
    headers = [e.data for e in log.events if e.type == "request/header"]
    assert [h["reason"] for h in headers] == ["initial", "change"]
    assert headers[1]["header"]["config"]["mode"] == "plan"


def test_builder_build_passes_session_log(monkeypatch):
    builder = AgentBuilder(cwd=".", provider=RecordingProvider([]))
    builder.with_model("test")
    # Avoid shell detection spawning a subprocess during the test.
    monkeypatch.setattr(builder, "_render_system_prompt", lambda tools: "PROMPT")
    log = make_log()
    agent = builder.build(session_log=log)
    assert agent.session_log is log
    assert agent.mode == "default"


@pytest.mark.asyncio
async def test_compaction_logged_and_replayable():
    """Compaction is logged as a surface-replace event, and the log reconstructs
    the exact request the model saw (the D7 invariant)."""
    log = make_log()
    bridge = ToolBridge()
    bridge.register(EchoTool())
    provider = CompactingProvider([
        LLMResponse(tool_calls=[ToolCall(id="c1", name="echo", input={"message": "x"})], stop_reason="tool_use"),
        LLMResponse(text="A" * 2000, stop_reason="end_turn"),
        LLMResponse(text="done", stop_reason="end_turn"),
    ])
    agent = AgentLoop(
        model="test", provider=provider, tools=bridge,
        system_prompt="SYS", session_log=log, context_window=400,
    )
    await agent.run("first")
    result = await agent.run("second", history=log.derive_messages())

    # One compaction event: a user/message with source "compaction" that shadows
    # the middle (assistant tool-call + tool-result) via a ReplaceOp.
    compactions = [
        e for e in log.events
        if e.type == "user/message" and e.data.get("source") == "compaction"
    ]
    assert len(compactions) == 1
    comp = compactions[0]
    assert isinstance(comp.surfaceOp, ReplaceOp)
    assert comp.sourceEventSeqs  # the shadowed surface seqs are recorded
    assert "MIDDLE-SUMMARY" in comp.data["message"]["content"]

    # Dual-write consistency: the returned history equals the projected surface.
    assert result.history == log.derive_messages()

    # D7: fold header (system) + surface == the exact request. RecordingProvider
    # stores a reference to the live messages list, which by now equals the final
    # messages (system + full surface).
    header_system = fold_request_header(iter(log.events))["system"]
    assert (
        [{"role": "system", "content": header_system}] + log.derive_messages()
        == provider.calls[-1][0]
    )


def test_mode_injection_on_change_and_invalidate(monkeypatch):
    builder = AgentBuilder(cwd=".", provider=RecordingProvider([]))
    # Neutralize memory/skills so only the mode injection is exercised.
    monkeypatch.setattr(builder, "render_injections", lambda: [])

    # Default mode on the first turn injects nothing (baseline in the prompt).
    assert builder.injections_for_turn() == []

    # Switching to plan narrates the change and injects the plan block.
    builder.with_mode("plan")
    inj = builder.injections_for_turn()
    assert len(inj) == 1 and inj[0][0] == "mode"
    assert "[Mode changed default → plan]" in inj[0][1]
    assert "<plan_mode>" in inj[0][1]

    # Unchanged mode: no further injection.
    assert builder.injections_for_turn() == []

    # Invalidate (e.g. after compaction): the current mode is re-injected.
    builder.invalidate_injections()
    inj = builder.injections_for_turn()
    assert len(inj) == 1 and inj[0][0] == "mode"
    assert "<plan_mode>" in inj[0][1]

    # Switching back to default is narrated as a change, with no block.
    builder.with_mode("default")
    inj = builder.injections_for_turn()
    assert len(inj) == 1 and inj[0][0] == "mode"
    assert "[Mode changed plan → default" in inj[0][1]
    assert "restored" in inj[0][1]
    assert "<plan_mode>" not in inj[0][1]


@pytest.mark.asyncio
async def test_repeat_reminder_logged_as_loop_guard_message():
    """The doom-loop reminder is logged as a source:"loop-guard" user/message,
    so derive_messages() reconstructs the exact model-visible request (D7)."""
    log = make_log()
    bridge = ToolBridge()
    bridge.register(EchoTool())

    class RepeatingProvider(RecordingProvider):
        async def chat(self, messages, tools, *, on_delta=None, on_thinking=None):
            self.calls.append((messages, tools))
            if len(self.calls) <= 3:
                return LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id=f"c{len(self.calls)}",
                            name="echo",
                            input={"message": "x"},
                        )
                    ],
                    stop_reason="tool_use",
                )
            return LLMResponse(text="done", stop_reason="end_turn")

    agent = AgentLoop(
        model="test",
        provider=RepeatingProvider([]),
        tools=bridge,
        system_prompt="s",
        session_log=log,
    )
    result = await agent.run("echo it")
    assert result.text == "done"

    loop_guard = [
        e for e in log.events
        if e.type == "user/message" and e.data.get("source") == "loop-guard"
    ]
    assert len(loop_guard) == 1
    assert (
        "repeating the exact same tool call"
        in loop_guard[0].data["message"]["content"]
    )

    # Model-visible ⟺ logged: the reminder is part of the derived surface.
    derived = log.derive_messages()
    assert any(
        m.get("role") == "user"
        and "repeating the exact same tool call" in m.get("content", "")
        for m in derived
    )

