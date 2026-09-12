"""Tests for the compaction surface-generation guard (P0-4).

The summarizer is an ``await``: the message indices a compaction edit is
prepared from can go stale while it runs. These tests pin the guard that
refuses to commit such an edit — and recomputes from the log's current surface
instead — so the logged surface and the request actually sent can never drift
apart (the "model-visible ⟺ logged" invariant).
"""

import json
from typing import Any

import pytest

from cluxmate.core.agent import AgentLoop
from cluxmate.core.providers.base import LLMResponse
from cluxmate.core.session_log import APPEND, SessionHeader, SessionLog
from cluxmate.tools.base import BaseTool, ToolBridge


class NoopTool(BaseTool):
    """Registered only so a main request carries a non-empty tool list — the
    distinguisher this suite's provider uses to tell a summarizer call apart."""

    @property
    def name(self) -> str:
        return "noop"

    @property
    def description(self) -> str:
        return "No-op."

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self) -> str:
        return "ok"


class MutatingProvider:
    """Answers the summarizer and, for the first ``mutations`` calls, appends a
    message to the session log while the summarizer is 'running'."""

    def __init__(self, log: SessionLog, mutations: int = 1):
        self.log = log
        self.mutations = mutations
        self.summarize_calls = 0
        self.requests: list[list[dict[str, Any]]] = []

    async def chat(self, messages, tools, *, on_delta=None, on_thinking=None):
        # compact() summarizes with an empty tool list — that identifies it.
        if not tools:
            self.summarize_calls += 1
            if self.summarize_calls <= self.mutations:
                self.log.append(
                    "user/message",
                    {"message": {"role": "user", "content": "INJECTED"},
                     "source": "hook"},
                    surface_op=APPEND,
                )
            return LLMResponse(text="MIDDLE-SUMMARY", stop_reason="end_turn")
        self.requests.append(messages)
        return LLMResponse(text="done", stop_reason="end_turn")

    def assistant_message_to_api(self, msg) -> dict[str, Any]:
        return {"role": "assistant", "content": msg.text or ""}

    def tool_result_to_api(self, result) -> dict[str, Any]:
        return {"role": "tool", "tool_call_id": result.tool_call_id,
                "content": result.content}

    def max_tokens(self) -> int:
        return 1000


def _seed_log() -> SessionLog:
    """A finished turn whose next request is comfortably over the window."""
    log = SessionLog.create(SessionHeader(id="s1", createdAt=0, apiType="openai"))
    log.append("turn/start", {"turn": 1})
    log.append(
        "user/message",
        {"message": {"role": "user", "content": "original task"}, "source": "human"},
        surface_op=APPEND,
    )
    log.append(
        "assistant/message",
        {"turn": 1, "step": 1,
         "message": {"role": "assistant", "content": "A" * 4000}},
        surface_op=APPEND,
    )
    log.append(
        "user/message",
        {"message": {"role": "user", "content": "B" * 2000}, "source": "human"},
        surface_op=APPEND,
    )
    log.append(
        "assistant/message",
        {"turn": 1, "step": 1, "message": {"role": "assistant", "content": "ok"}},
        surface_op=APPEND,
    )
    return log


def _agent(log: SessionLog, provider: MutatingProvider) -> AgentLoop:
    bridge = ToolBridge()
    bridge.register(NoopTool())
    return AgentLoop(
        model="test", provider=provider, tools=bridge,
        system_prompt="SYS", session_log=log, context_window=1000,
    )


def _compactions(log: SessionLog) -> list:
    return [
        e for e in log.events
        if e.type == "user/message" and e.data.get("source") == "compaction"
    ]


def _last_reason(log: SessionLog) -> dict:
    return log.events[-1].data["reason"]


async def _run_turn(log: SessionLog, provider: MutatingProvider):
    return await _agent(log, provider).run(
        "second", history=log.derive_messages()
    )


# ── the generation counter itself ──────────────────────────


def test_surface_generation_counts_surface_mutations_only():
    log = _seed_log()
    # 5 surface events appended (and one log-only turn/start, which is not a
    # surface message and must not count).
    assert log.surface_generation == 4
    log.append("step/start", {"turn": 1, "step": 1})
    assert log.surface_generation == 4
    log.append("usage", {"turn": 1, "step": 1})
    assert log.surface_generation == 4


def test_surface_generation_counts_replace_ops_and_survives_replay():
    log = _seed_log()
    surface = log.surface
    before = log.surface_generation
    shadowed = [e.seq for e in surface[1:3]]
    log.append(
        "user/message",
        {"message": {"role": "user", "content": "summary"}, "source": "compaction"},
        surface_op=_replace(1, 2),
        source_event_seqs=shadowed,
    )
    assert log.surface_generation == before + 1
    assert len(log.surface) == len(surface) - 1
    # A rebuilt log replays the same ops and lands on the same count.
    assert SessionLog.from_events(log.header, log.events).surface_generation == (
        log.surface_generation
    )


def _replace(start: int, end: int):
    from cluxmate.core.session_log import ReplaceOp

    return ReplaceOp(start=start, end=end)


# ── the guard ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_compaction_is_recomputed_and_injected_message_kept():
    log = _seed_log()
    provider = MutatingProvider(log, mutations=1)

    result = await _run_turn(log, provider)

    # First summarize went stale, so the compaction was recomputed (2 calls).
    assert provider.summarize_calls == 2
    assert len(_compactions(log)) == 1
    # The message appended mid-summarize is on the surface AND in the request:
    # the recompute re-derived the input from the log instead of committing an
    # edit prepared against the pre-append surface. (Verified to fail without the
    # guard: the append is logged but never reaches the model.)
    assert "INJECTED" in [
        m["content"] for m in log.derive_messages() if isinstance(m.get("content"), str)
    ]
    assert provider.requests[-1] == [{"role": "system", "content": "SYS"}] + (
        log.derive_messages()
    )
    assert result.history == log.derive_messages()
    assert _last_reason(log)["compaction"] == {"stale_retries": 1}


@pytest.mark.asyncio
async def test_clean_compaction_commits_on_the_first_summary():
    log = _seed_log()
    provider = MutatingProvider(log, mutations=0)

    await _run_turn(log, provider)

    assert provider.summarize_calls == 1
    assert len(_compactions(log)) == 1
    assert provider.requests[-1] == [{"role": "system", "content": "SYS"}] + (
        log.derive_messages()
    )
    assert "compaction" not in _last_reason(log)


@pytest.mark.asyncio
async def test_surface_that_keeps_moving_never_commits_a_stale_edit():
    log = _seed_log()
    provider = MutatingProvider(log, mutations=99)

    result = await _run_turn(log, provider)

    # Both attempts went stale: no compaction is logged and the step simply
    # stays over budget (the next step would retry) — never a desynced surface.
    assert provider.summarize_calls == 2  # 1 attempt + MAX_COMPACTION_RECOMPUTES
    assert _compactions(log) == []
    assert provider.requests[-1] == [{"role": "system", "content": "SYS"}] + (
        log.derive_messages()
    )
    assert result.history == log.derive_messages()
    assert _last_reason(log)["compaction"] == {"stale_retries": 2}


@pytest.mark.asyncio
async def test_stale_retries_reset_each_turn():
    log = _seed_log()
    provider = MutatingProvider(log, mutations=99)
    agent = _agent(log, provider)

    await agent.run("second", history=log.derive_messages())
    await agent.run("third", history=log.derive_messages())

    # The counter is per-turn: the second turn reports only its own retries.
    assert _last_reason(log)["compaction"] == {"stale_retries": 2}


def test_replace_op_round_trips_through_jsonl():
    op = _replace(0, 1)
    from cluxmate.core.session_log import _surface_op_from_json, _surface_op_to_json

    assert _surface_op_from_json(_surface_op_to_json(op)) == op
