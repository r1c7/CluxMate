"""Tests for the ask_user_question JSON-RPC gate (JsonRpcCallbacks.ask_question).

Two ways the desktop could end up with a question the user cannot answer —
and therefore a turn that never finishes:

* the answer landing while the ``question`` event is still being written
  (the waiter was registered after the emit, so the answer was stored and
  never consumed -> ``evt.wait()`` blocks forever);
* two ``ask_user_question`` calls in one tool batch (the loop executes a
  step's calls concurrently, but every front-end renders ONE question batch
  at a time, so the second emit overwrote the first card).
"""

import asyncio
import threading

import pytest

from cluxmate.core import jsonrpc_server
from cluxmate.core.jsonrpc_server import JsonRpcCallbacks


class _Emitter:
    """Stands in for the stdout writer, recording the question events emitted."""

    def __init__(self, monkeypatch):
        self.call_ids: list[str] = []
        self.on_question = None          # optional hook running inside the emit
        monkeypatch.setattr(jsonrpc_server, "_write_dict", self)

    def __call__(self, payload: dict):
        params = payload.get("params") or {}
        if params.get("type") != "question":
            return
        call_id = params["call_id"]
        self.call_ids.append(call_id)
        if self.on_question is not None:
            self.on_question(call_id)


@pytest.mark.asyncio
async def test_answer_arriving_during_emission_is_consumed(monkeypatch):
    """An answer that lands while the question event is being written must not
    be lost — the waiter has to be registered before the emit."""
    cbs = JsonRpcCallbacks(None)
    emitter = _Emitter(monkeypatch)

    def answer_immediately(call_id: str):
        cbs.resolve_question(call_id, [{"id": "q", "selected": ["A"]}])

    emitter.on_question = answer_immediately

    try:
        result = await asyncio.wait_for(
            cbs.ask_question([{"id": "q", "question": "Which?"}], "call-1"),
            timeout=5,
        )
    finally:
        cbs.cancel()          # never leave a pool thread parked in evt.wait()

    assert emitter.call_ids == ["call-1"]
    assert result == {"answers": [{"id": "q", "selected": ["A"]}]}


@pytest.mark.asyncio
async def test_concurrent_questions_are_emitted_one_at_a_time(monkeypatch):
    """A model that splits its questions across two calls in one batch must not
    have the second card replace the first: the second waits for the first."""
    cbs = JsonRpcCallbacks(None)
    emitter = _Emitter(monkeypatch)

    first = asyncio.create_task(
        cbs.ask_question([{"id": "q1", "question": "First?"}], "c1")
    )
    second = asyncio.create_task(
        cbs.ask_question([{"id": "q2", "question": "Second?"}], "c2")
    )
    try:
        await asyncio.sleep(0.3)
        assert emitter.call_ids == ["c1"]

        cbs.resolve_question("c1", [{"id": "q1", "selected": ["A"]}])
        assert (await asyncio.wait_for(first, timeout=5))["answers"][0]["selected"] == ["A"]

        await asyncio.sleep(0.3)
        assert emitter.call_ids == ["c1", "c2"]

        cbs.resolve_question("c2", [{"id": "q2", "selected": ["B"]}])
        assert (await asyncio.wait_for(second, timeout=5))["answers"][0]["selected"] == ["B"]
    finally:
        cbs.cancel()
        for task in (first, second):
            task.cancel()


@pytest.mark.asyncio
async def test_cancel_releases_a_waiting_question(monkeypatch):
    """Stop must settle the question (and the serialization lock) instead of
    leaving the turn and its pool thread parked forever."""
    cbs = JsonRpcCallbacks(None)
    _Emitter(monkeypatch)

    task = asyncio.create_task(
        cbs.ask_question([{"id": "q", "question": "Which?"}], "call-1")
    )
    await asyncio.sleep(0.2)
    cbs.cancel()

    with pytest.raises(jsonrpc_server._CancelledError):
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_cancel_releases_the_serialization_lock(monkeypatch):
    """A question queued behind a cancelled one must not deadlock the batch."""
    cbs = JsonRpcCallbacks(None)
    emitter = _Emitter(monkeypatch)

    first = asyncio.create_task(
        cbs.ask_question([{"id": "q1", "question": "First?"}], "c1")
    )
    second = asyncio.create_task(
        cbs.ask_question([{"id": "q2", "question": "Second?"}], "c2")
    )
    await asyncio.sleep(0.2)
    cbs.cancel()

    for task in (first, second):
        with pytest.raises(jsonrpc_server._CancelledError):
            await asyncio.wait_for(task, timeout=5)
    assert emitter.call_ids == ["c1"]


def test_resolve_question_is_thread_safe():
    """question/answer arrives on the dispatch thread while the turn's loop
    waits on an executor thread — the handoff must work across threads."""
    cbs = JsonRpcCallbacks(None)
    evt = threading.Event()
    cbs._question_events["call-1"] = evt

    t = threading.Thread(
        target=cbs.resolve_question, args=("call-1", [{"id": "q", "selected": ["A"]}])
    )
    t.start()
    assert evt.wait(timeout=5) is True
    t.join(timeout=5)
    assert cbs._question_answers["call-1"] == {"answers": [{"id": "q", "selected": ["A"]}]}
