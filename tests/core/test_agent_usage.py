"""A turn's token accounting must survive every way a turn can end.

The desktop's session footer sums, per reply, the reply's prompt tokens and
completion tokens. Until this contract existed only a normal end_turn reply
carried them: a max-turns stop returned no ``cache_usage`` at all, a provider
failure returned neither usage nor timing, and an interrupted/aborted turn —
which never returns an AgentResult, because ``run()`` re-raises — carried them
nowhere, so those turns counted as free in the footer while still being billed.

Two halves:
  * `AgentResult` on every terminal path (so the RPC response and the subagent
    report are complete), and
  * `AgentLoop.turn_usage` + the live ``on_usage`` callback (so a caller that
    gets no result at all can still read what the turn spent).
"""

import asyncio
import time

import pytest

from cluxmate.core.agent import AgentCallbacks, AgentLoop
from cluxmate.core.providers.base import (
    LLMNetworkError,
    LLMProviderError,
    LLMResponse,
    ToolCall,
)
from cluxmate.core.session_log import SessionHeader, SessionLog
from cluxmate.tools.base import ToolBridge
from cluxmate.tools.todo import TodoTool


class FakeProvider:
    """Returns queued responses; an Exception instance in the queue is raised."""

    def __init__(self, responses: list) -> None:
        self.responses = responses
        self.calls = 0

    async def chat(self, messages, tools, *, on_delta=None, on_thinking=None):
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def assistant_message_to_api(self, msg):
        return {"role": "assistant", "content": msg.text or ""}

    def tool_result_to_api(self, result):
        return {"role": "user", "content": result.content}

    def max_tokens(self) -> int:
        return 1000


class UsageRecorder(AgentCallbacks):
    """Collects every live on_usage payload in arrival order."""

    def __init__(self) -> None:
        self.updates: list[dict] = []

    async def on_usage(self, usage) -> None:
        self.updates.append(dict(usage))


def _agent(provider, *, max_turns=None) -> tuple[AgentLoop, SessionLog]:
    log = SessionLog.create(
        SessionHeader(id="usage-s1", createdAt=0, apiType="openai")
    )
    bridge = ToolBridge()
    bridge.register(TodoTool())
    return (
        AgentLoop(
            model="test",
            provider=provider,
            tools=bridge,
            system_prompt="s",
            session_log=log,
            max_turns=max_turns,
        ),
        log,
    )


def _usage_call(text, *, in_tok, out_tok, cache_read=0):
    return LLMResponse(
        text=text,
        stop_reason="end_turn",
        input_tokens=in_tok,
        output_tokens=out_tok,
        cache_read_input_tokens=cache_read,
    )


def _tool_call(call_id, *, in_tok, out_tok):
    return LLMResponse(
        tool_calls=[
            ToolCall(
                id=call_id,
                name="todo_write",
                input={"todos": [{"content": "x", "status": "in_progress"}]},
            )
        ],
        stop_reason="tool_use",
        input_tokens=in_tok,
        output_tokens=out_tok,
    )


@pytest.mark.asyncio
async def test_max_turns_result_still_reports_the_usage_it_spent():
    """The safety stop used to drop cache_usage entirely (measured: a real
    session lost 41M prompt tokens on one max-turns turn)."""
    provider = FakeProvider([_tool_call("t1", in_tok=100, out_tok=10)] * 2)
    agent, _ = _agent(provider, max_turns=2)

    result = await agent.run("go")

    assert result.text == "[Agent loop exceeded maximum turns]"
    assert result.cache_usage == {
        "input_tokens": 200,
        "cache_read": 0,
        "cache_write": 0,
    }
    assert result.out_tokens == 20
    assert agent.turn_usage["input_tokens"] == 200


@pytest.mark.asyncio
async def test_provider_failure_result_reports_the_usage_accrued_so_far():
    """Calls made before the failure were billed — the error reply must carry
    them (it previously returned a zeroed usage + timing)."""
    provider = FakeProvider([
        _tool_call("t1", in_tok=5000, out_tok=40),
        LLMNetworkError("boom"),
    ])
    agent, _ = _agent(provider)

    result = await agent.run("go")

    assert result.cache_usage["input_tokens"] == 5000
    assert result.out_tokens == 40


@pytest.mark.asyncio
async def test_provider_error_result_reports_usage_and_keeps_the_message():
    provider = FakeProvider([
        _tool_call("t1", in_tok=700, out_tok=7),
        LLMProviderError("Insufficient Balance"),
    ])
    agent, _ = _agent(provider)

    result = await agent.run("go")

    assert result.text == "Insufficient Balance"
    assert result.cache_usage["input_tokens"] == 700
    assert result.out_tokens == 7


@pytest.mark.asyncio
async def test_on_usage_fires_per_call_with_cumulative_totals():
    provider = FakeProvider([
        _tool_call("t1", in_tok=100, out_tok=10),
        _usage_call("done", in_tok=250, out_tok=20, cache_read=200),
    ])
    agent, _ = _agent(provider)
    rec = UsageRecorder()

    await agent.run("go", callbacks=rec)

    assert [u["input_tokens"] for u in rec.updates] == [100, 350]
    assert [u["output_tokens"] for u in rec.updates] == [10, 30]
    assert rec.updates[-1]["cache_read"] == 200


@pytest.mark.asyncio
async def test_on_usage_carries_turn_step_and_time():
    """The event must say WHICH call published it: the session turn, the step,
    and the emission time (epoch ms). Without them a front-end cannot tell a
    superseded turn's late event from the one it should apply."""
    provider = FakeProvider([
        _tool_call("t1", in_tok=100, out_tok=10),
        _usage_call("done", in_tok=250, out_tok=20),
    ])
    agent, _ = _agent(provider)
    rec = UsageRecorder()
    before = int(time.time() * 1000)

    await agent.run("go", callbacks=rec)

    assert [u["turn"] for u in rec.updates] == [1, 1]
    assert [u["step"] for u in rec.updates] == [1, 2]
    for u in rec.updates:
        assert isinstance(u["time"], int)
        assert before <= u["time"] <= before + 60_000


@pytest.mark.asyncio
async def test_cancelled_turn_keeps_the_totals_on_the_loop():
    """An interrupted turn re-raises out of run() — no result exists, so the
    live callback / turn_usage are the only record the front-end can use."""
    provider = FakeProvider([
        _tool_call("t1", in_tok=900, out_tok=90),
        asyncio.CancelledError(),
    ])
    agent, _ = _agent(provider)
    rec = UsageRecorder()

    with pytest.raises(asyncio.CancelledError):
        await agent.run("go", callbacks=rec)

    assert rec.updates[-1]["input_tokens"] == 900
    assert rec.updates[-1]["output_tokens"] == 90
    assert agent.turn_usage["input_tokens"] == 900
    assert agent.turn_usage["output_tokens"] == 90


@pytest.mark.asyncio
async def test_turn_usage_resets_between_turns():
    """One AgentLoop serves a whole session: turn 2's totals must not inherit
    turn 1's (the accumulator is instance state now)."""
    provider = FakeProvider([
        _usage_call("one", in_tok=1000, out_tok=100),
        _usage_call("two", in_tok=30, out_tok=3),
    ])
    agent, _ = _agent(provider)

    await agent.run("first")
    result = await agent.run("second")

    assert result.cache_usage["input_tokens"] == 30
    assert result.out_tokens == 3
    assert agent.turn_usage["input_tokens"] == 30


def test_empty_turn_usage_shape_is_the_documented_contract():
    from cluxmate.core.agent import _empty_turn_usage

    assert _empty_turn_usage() == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read": 0,
        "cache_write": 0,
        "ttft_ms": None,
        "gen_ms": 0,
    }
