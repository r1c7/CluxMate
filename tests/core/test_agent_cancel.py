"""A turn that ends while a tool is still running must stop that tool.

``run()`` re-raises on cancellation (and the JSON-RPC layer abandons the turn
thread), so the executor thread blocked inside the tool is orphaned: nothing
would ever notice that the tool — a `pytest`, an `npm run`, a process tree — is
still running. The turn's own teardown is the last chance, and it reaches the
tool through ``ToolBridge.cancel_running()``.
"""

import asyncio

import pytest

from cluxmate.core.agent import AgentLoop
from cluxmate.core.providers.base import LLMResponse, ToolCall
from cluxmate.core.session_log import SessionHeader, SessionLog
from cluxmate.tools.base import BaseTool, ToolBridge


class FakeProvider:
    """Hands out queued responses; never needs a second one in these tests."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)

    async def chat(self, messages, tools, *, on_delta=None, on_thinking=None):
        return self.responses.pop(0)

    def assistant_message_to_api(self, msg):
        return {"role": "assistant", "content": msg.text or ""}

    def tool_result_to_api(self, result):
        return {"role": "tool", "tool_call_id": result.tool_call_id,
                "content": result.content}

    def max_tokens(self) -> int:
        return 1000


class BlockingTool(BaseTool):
    """Runs until cancelled — stands in for a long shell command."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancel_calls = 0

    @property
    def name(self) -> str:
        return "slow_command"

    @property
    def description(self) -> str:
        return "Runs until the turn ends."

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> str:
        self.started.set()
        await asyncio.sleep(3600)
        return "unreachable"

    def cancel_running(self) -> None:
        self.cancel_calls += 1


def _agent(tool: BaseTool) -> tuple[AgentLoop, SessionLog]:
    log = SessionLog.create(
        SessionHeader(id="cancel-s1", createdAt=0, apiType="openai")
    )
    bridge = ToolBridge()
    bridge.register(tool)
    return (
        AgentLoop(
            model="test",
            provider=FakeProvider([LLMResponse(
                tool_calls=[ToolCall(id="c1", name=tool.name, input={})],
                stop_reason="tool_use",
            )]),
            tools=bridge,
            system_prompt="s",
            session_log=log,
        ),
        log,
    )


@pytest.mark.asyncio
async def test_cancelled_turn_cancels_the_running_tool():
    tool = BlockingTool()
    agent, log = _agent(tool)

    task = asyncio.create_task(agent.run("go"))
    await asyncio.wait_for(tool.started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert tool.cancel_calls == 1
    # The abandoned call still gets its terminal result on the surface, so a
    # replay never shows a tool card stuck in "running".
    texts = [str(m.get("content", "")) for m in log.derive_messages()]
    assert any("cancelled" in t for t in texts), texts


@pytest.mark.asyncio
async def test_teardown_cancel_runs_after_the_tools_not_during_them():
    """The hook belongs to the END of the turn: if it fired while a call was
    still in flight it would kill work the loop is still waiting for."""

    class OrderedTool(BlockingTool):
        def __init__(self) -> None:
            super().__init__()
            self.order: list[str] = []

        async def execute(self, **kwargs) -> str:
            self.order.append("execute")
            return "done"

        def cancel_running(self) -> None:
            super().cancel_running()
            self.order.append("cancel")

    tool = OrderedTool()
    bridge = ToolBridge()
    bridge.register(tool)
    log = SessionLog.create(
        SessionHeader(id="cancel-s2", createdAt=0, apiType="openai")
    )
    agent = AgentLoop(
        model="test",
        provider=FakeProvider([
            LLMResponse(
                tool_calls=[ToolCall(id="c1", name=tool.name, input={})],
                stop_reason="tool_use",
            ),
            LLMResponse(text="finished", stop_reason="end_turn"),
        ]),
        tools=bridge,
        system_prompt="s",
        session_log=log,
    )

    result = await agent.run("go")

    assert result.text == "finished"
    assert tool.order == ["execute", "cancel"]
