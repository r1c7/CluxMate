"""Per-subagent turn budget (AgentLoop.max_turns + AgentResult.turns)."""

import pytest

from cluxmate.core.agent import AgentLoop
from cluxmate.core.providers.base import LLMResponse, ToolCall
from cluxmate.core.session_log import SessionHeader, SessionLog
from cluxmate.tools.base import BaseTool, ToolBridge


class _EchoTool(BaseTool):
    @property
    def name(self):
        return "echo"

    @property
    def description(self):
        return "Echo."

    @property
    def input_schema(self):
        return {"type": "object", "properties": {}}

    async def execute(self):
        return "ok"


class _LoopingProvider:
    """Always asks for another tool call, so the loop only stops at the cap."""

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, tools, *, on_delta=None, on_thinking=None):
        self.calls += 1
        return LLMResponse(
            tool_calls=[ToolCall(id=f"c{self.calls}", name="echo", input={})],
            stop_reason="tool_use",
        )

    def assistant_message_to_api(self, msg):
        return {"role": "assistant", "content": msg.text or ""}

    def tool_result_to_api(self, result):
        return {"role": "tool", "tool_call_id": result.tool_call_id, "content": result.content}

    def max_tokens(self):
        return 1000


def _log() -> SessionLog:
    return SessionLog.create(SessionHeader(id="s1", createdAt=0, apiType="openai"))


@pytest.mark.asyncio
async def test_max_turns_stops_the_loop_and_is_reported():
    log = _log()
    bridge = ToolBridge()
    bridge.register(_EchoTool())
    agent = AgentLoop(
        model="test", provider=_LoopingProvider(), tools=bridge,
        system_prompt="s", session_log=log, cwd=None, max_turns=3,
    )
    result = await agent.run("go")
    assert result.turns == 3
    assert log.events[-1].type == "turn/end"
    assert log.events[-1].data["reason"]["kind"] == "max-turns"


@pytest.mark.asyncio
async def test_default_max_turns_is_unchanged():
    agent = AgentLoop(
        model="test", provider=_LoopingProvider(), tools=ToolBridge(),
        system_prompt="s", session_log=_log(),
    )
    assert agent.max_turns == AgentLoop.MAX_TURNS == 150


@pytest.mark.asyncio
async def test_completed_turn_counts_one_turn():
    class _Once:
        def __init__(self):
            self.calls = 0

        async def chat(self, messages, tools, *, on_delta=None, on_thinking=None):
            self.calls += 1
            return LLMResponse(text="done", stop_reason="end_turn")

        def assistant_message_to_api(self, msg):
            return {"role": "assistant", "content": msg.text or ""}

        def tool_result_to_api(self, result):
            return {"role": "tool", "tool_call_id": result.tool_call_id, "content": result.content}

        def max_tokens(self):
            return 1000

    agent = AgentLoop(
        model="test", provider=_Once(), tools=ToolBridge(),
        system_prompt="s", session_log=_log(),
    )
    result = await agent.run("hi")
    assert result.turns == 1
