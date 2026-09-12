"""Deterministic live todo_update emission for batches of todo_write calls.

A step's tool calls run concurrently (asyncio.gather), so per-call emission of
todo_update events could surface an older snapshot AFTER a newer one — a task
the model just marked `completed` re-appearing as `in_progress` when a parallel
todo_write settles last — and the live order could disagree with the log's
input-order fold. The loop must emit exactly ONE update per step: the last
executed todo_write in input order, the same winner the todo/write fold picks.
"""

import asyncio

import pytest

from cluxmate.core.agent import AgentCallbacks, AgentLoop
from cluxmate.core.providers.base import LLMResponse, ToolCall
from cluxmate.core.session_log import SessionHeader, SessionLog, fold_todos
from cluxmate.tools.base import ToolBridge
from cluxmate.tools.todo import TodoTool


class _TodoRecorder(AgentCallbacks):
    """Collects every on_todo_update payload in arrival order."""

    def __init__(self) -> None:
        self.updates: list[list[dict]] = []

    async def on_todo_update(self, todos) -> None:
        self.updates.append(list(todos))


class _SlowTodoTool(TodoTool):
    """A todo_write whose execute sleeps when the list contains `marker`,
    so a parallel sibling settles first (execution order != input order)."""

    def __init__(self, marker: str, delay: float = 0.1) -> None:
        super().__init__()
        self._marker = marker
        self._delay = delay

    async def execute(self, todos=None, **kwargs):
        if todos and any(
            isinstance(t, dict) and t.get("content") == self._marker for t in todos
        ):
            await asyncio.sleep(self._delay)
        return await super().execute(todos=todos, **kwargs)


class FakeProvider:
    def __init__(self, responses: list[LLMResponse]):
        self.responses = responses

    async def chat(self, messages, tools, *, on_delta=None, on_thinking=None):
        return self.responses.pop(0)

    def assistant_message_to_api(self, msg):
        return {"role": "assistant", "content": msg.text or ""}

    def tool_result_to_api(self, result):
        return {"role": "user", "content": result.content}

    def max_tokens(self) -> int:
        return 1000


def _agent(provider, tools, recorder=None) -> AgentLoop:
    log = SessionLog.create(
        SessionHeader(id="batch-s1", createdAt=0, apiType="openai")
    )
    return AgentLoop(
        model="test",
        provider=provider,
        tools=tools,
        system_prompt="s",
        session_log=log,
    ), log


@pytest.mark.asyncio
async def test_parallel_todo_writes_emit_one_update_last_input_wins():
    """Two todo_writes in one batch: the live update is the LAST one in input
    order (the fold's winner), emitted exactly once — never the stale earlier
    snapshot, regardless of execution completion order."""
    slow = _SlowTodoTool("task a")
    bridge = ToolBridge()
    bridge.register(slow)
    recorder = _TodoRecorder()
    provider = FakeProvider([
        LLMResponse(
            tool_calls=[
                ToolCall(  # slow: settles LAST even though it is first
                    id="t1", name="todo_write",
                    input={"todos": [{"content": "task a", "status": "in_progress"}]},
                ),
                ToolCall(  # fast: settles FIRST
                    id="t2", name="todo_write",
                    input={"todos": [
                        {"content": "task a", "status": "completed"},
                        {"content": "task b", "status": "in_progress"},
                    ]},
                ),
            ],
            stop_reason="tool_use",
        ),
        LLMResponse(text="All set.", stop_reason="end_turn"),
    ])
    agent, log = _agent(provider, bridge, recorder)

    result = await agent.run("Do it", callbacks=recorder)

    assert result.text == "All set."
    # One deterministic update carrying the final (input-order-last) list.
    assert recorder.updates == [[
        {"content": "task a", "status": "completed"},
        {"content": "task b", "status": "in_progress"},
    ]]
    # The live update and the persisted fold are the SAME list.
    assert fold_todos(log.events) == recorder.updates[-1]


@pytest.mark.asyncio
async def test_batch_errored_last_todo_write_emits_last_good():
    """A batch whose LAST todo_write fails validation still emits the last
    GOOD list — matching the log fold, which also skips the failed call."""
    bridge = ToolBridge()
    bridge.register(TodoTool())
    recorder = _TodoRecorder()
    provider = FakeProvider([
        LLMResponse(
            tool_calls=[
                ToolCall(
                    id="t1", name="todo_write",
                    input={"todos": [{"content": "task a", "status": "in_progress"}]},
                ),
                ToolCall(  # invalid: duplicate content → error result
                    id="t2", name="todo_write",
                    input={"todos": [
                        {"content": "dup", "status": "pending"},
                        {"content": "dup", "status": "completed"},
                    ]},
                ),
            ],
            stop_reason="tool_use",
        ),
        LLMResponse(text="Fixed it.", stop_reason="end_turn"),
    ])
    agent, log = _agent(provider, bridge, recorder)

    result = await agent.run("Do it", callbacks=recorder)

    assert result.text == "Fixed it."
    assert recorder.updates == [[{"content": "task a", "status": "in_progress"}]]
    assert fold_todos(log.events) == recorder.updates[-1]


@pytest.mark.asyncio
async def test_sequential_todo_steps_emit_per_step_updates():
    """The common sequential flow is unchanged: one update per step, in step
    order, ending on the newest list."""
    bridge = ToolBridge()
    bridge.register(TodoTool())
    recorder = _TodoRecorder()
    provider = FakeProvider([
        LLMResponse(
            tool_calls=[
                ToolCall(
                    id="t1", name="todo_write",
                    input={"todos": [{"content": "task a", "status": "pending"}]},
                ),
            ],
            stop_reason="tool_use",
        ),
        LLMResponse(
            tool_calls=[
                ToolCall(
                    id="t2", name="todo_write",
                    input={"todos": [{"content": "task a", "status": "completed"}]},
                ),
            ],
            stop_reason="tool_use",
        ),
        LLMResponse(text="Done.", stop_reason="end_turn"),
    ])
    agent, log = _agent(provider, bridge, recorder)

    result = await agent.run("Do it", callbacks=recorder)

    assert result.text == "Done."
    assert recorder.updates == [
        [{"content": "task a", "status": "pending"}],
        [{"content": "task a", "status": "completed"}],
    ]
    assert fold_todos(log.events) == recorder.updates[-1]
