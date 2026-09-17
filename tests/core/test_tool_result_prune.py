"""Tier-0 tool-result pruning — the model-free pass that runs before compaction.

A stale, oversized tool result keeps its head and tail and loses its middle, so
an over-budget context can often be relieved without paying for a summarizer
call (DSH `compaction-tool-result-pruner`). These tests pin the four things that
make that safe:

* it is *pressure-gated* — a below-budget conversation is never touched, so the
  prompt prefix (and the provider's cache) stays stable in the common case;
* it is *age-gated* — the working set (this turn's results) and the previous
  turn's are never rewritten;
* it is *logged* — each rewrite is a single-node surface replace that preserves
  every other field, so replay, the audit trail and the ContextViewer all still
  see the real call;
* it *keeps the model and the UI in step* — the pruned text is what the model
  sees, and the same text is pushed to the front-end so its cards match.
"""

import pytest

from cluxmate.core.agent import AgentCallbacks, AgentLoop
from cluxmate.core.context import PRUNE_HEAD_CHARS, PRUNE_TAIL_CHARS
from cluxmate.core.providers.base import LLMResponse
from cluxmate.core.session_log import APPEND, ReplaceOp, SessionHeader, SessionLog
from cluxmate.tools.base import BaseTool, ToolBridge

PRUNED_MARKER = "characters pruned from the middle of this result"


class NoopTool(BaseTool):
    """Keeps the main request's tool list non-empty — that is how the provider
    above tells a summarizer call (no tools) apart from a real one."""

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


class RecordingProvider:
    """Records every request (snapshotted, so the assertion reads what was
    actually sent) and identifies the summarizer by its empty tool list."""

    def __init__(self):
        self.requests: list[list[dict]] = []
        self.summarize_requests: list[list[dict]] = []

    async def chat(self, messages, tools, *, on_delta=None, on_thinking=None):
        if not tools:
            self.summarize_requests.append(list(messages))
            return LLMResponse(text="MIDDLE-SUMMARY", stop_reason="end_turn")
        self.requests.append(list(messages))
        return LLMResponse(text="done", stop_reason="end_turn")

    def assistant_message_to_api(self, msg) -> dict:
        return {"role": "assistant", "content": msg.text or ""}

    def tool_result_to_api(self, result) -> dict:
        return {"role": "tool", "tool_call_id": result.tool_call_id,
                "content": result.content}

    def max_tokens(self) -> int:
        return 1000


class CollectingCallbacks(AgentCallbacks):
    def __init__(self):
        super().__init__()
        self.pruned: list[list[dict]] = []

    async def on_context_pruned(self, entries: list[dict]) -> None:
        self.pruned.append(entries)


def _big_result(size: int) -> str:
    """A result whose head, middle and tail are each identifiable."""
    return "H" * (PRUNE_HEAD_CHARS) + "M" * (size - PRUNE_HEAD_CHARS - PRUNE_TAIL_CHARS) \
        + "T" * PRUNE_TAIL_CHARS


def _seed_log(*, results: int = 1, result_turn: int = 1, size: int = 20_000) -> SessionLog:
    """Turn 1 holds ``results`` huge tool results; turn 2 is a completed turn.

    Running a third turn makes a ``result_turn=1`` result two turns old (the
    minimum age for pruning) while a ``result_turn=2`` one stays fresh.
    """
    log = SessionLog.create(SessionHeader(id="s1", createdAt=0, apiType="openai"))
    log.append("turn/start", {"turn": 1})
    log.append(
        "user/message",
        {"message": {"role": "user", "content": "original task"}, "source": "human"},
        surface_op=APPEND,
    )
    for i in range(results):
        call_id = f"c{i}"
        log.append(
            "assistant/message",
            {"turn": 1, "step": 1, "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": call_id, "type": "function",
                                "function": {"name": "read_file", "arguments": "{}"}}],
            }},
            surface_op=APPEND,
        )
        log.append(
            "tool/result",
            {"turn": result_turn, "step": 1, "callId": call_id,
             "message": {"role": "tool", "tool_call_id": call_id,
                         "content": _big_result(size)},
             # Audit metadata that a rewrite must preserve.
             "riskLevel": "safe", "decision": "auto"},
            surface_op=APPEND,
        )
    log.append(
        "assistant/message",
        {"turn": 1, "step": 1, "message": {"role": "assistant", "content": "done"}},
        surface_op=APPEND,
    )
    log.append("turn/start", {"turn": 2})
    log.append(
        "user/message",
        {"message": {"role": "user", "content": "second"}, "source": "human"},
        surface_op=APPEND,
    )
    log.append(
        "assistant/message",
        {"turn": 2, "step": 1, "message": {"role": "assistant", "content": "ok"}},
        surface_op=APPEND,
    )
    return log


def _agent(log: SessionLog, provider, window: int) -> AgentLoop:
    bridge = ToolBridge()
    bridge.register(NoopTool())
    return AgentLoop(
        model="test", provider=provider, tools=bridge,
        system_prompt="SYS", session_log=log, context_window=window,
    )


def _replace_events(log: SessionLog) -> list:
    return [e for e in log.events if e.type == "tool/result"
            and isinstance(e.surfaceOp, ReplaceOp)]


def _compactions(log: SessionLog) -> list:
    return [e for e in log.events
            if e.type == "user/message" and e.data.get("source") == "compaction"]


def _surface_texts(log: SessionLog) -> list[str]:
    return [m.get("content") for m in log.derive_messages()
            if isinstance(m.get("content"), str)]


async def _run(log: SessionLog, provider, window: int, callbacks=None):
    return await _agent(log, provider, window).run(
        "third", history=log.derive_messages(), callbacks=callbacks,
    )


# ── the prune itself ───────────────────────────────────────


@pytest.mark.asyncio
async def test_prune_alone_clears_pressure_without_a_summarizer_call():
    """window 2000 -> limit 1600 tokens: ~5000 pre-prune, ~1300 after."""
    log = _seed_log()
    provider = RecordingProvider()
    callbacks = CollectingCallbacks()

    result = await _run(log, provider, window=2_000, callbacks=callbacks)

    assert provider.summarize_requests == []
    assert _compactions(log) == []
    # The request the model actually got carries the pruned text, not the middle.
    sent = "\n".join(
        m.get("content") or "" for m in provider.requests[-1]
        if isinstance(m.get("content"), str)
    )
    assert PRUNED_MARKER in sent
    assert "M" * 100 not in sent
    assert result.history == log.derive_messages()
    assert len(callbacks.pruned) == 1
    assert [e["call_id"] for e in callbacks.pruned[0]] == ["c0"]
    # The front-end is told exactly what the model now sees.
    assert callbacks.pruned[0][0]["output"] == log.surface[2].data["message"]["content"]


@pytest.mark.asyncio
async def test_prune_is_logged_as_a_field_preserving_single_node_replace():
    log = _seed_log()
    provider = RecordingProvider()
    original = [e for e in log.events if e.type == "tool/result"][0]
    surface_before = [e.seq for e in log.surface]

    await _run(log, provider, window=2_000)

    replaces = _replace_events(log)
    assert len(replaces) == 1
    op = replaces[0].surfaceOp
    # One node in, one node out: the surface length (and every index) is stable.
    assert op.start == op.end == surface_before.index(original.seq)
    assert replaces[0].sourceEventSeqs == (original.seq,)
    # Only that one node changed: every other surface entry keeps its identity
    # (the two trailing entries are this turn's user message and reply).
    after = [e.seq for e in log.surface]
    assert after[:2] == surface_before[:2]
    assert after[3:len(surface_before)] == surface_before[3:]
    assert after[2] == replaces[0].seq
    rewritten = replaces[0].data
    assert rewritten["callId"] == "c0"
    assert rewritten["turn"] == 1 and rewritten["step"] == 1
    assert rewritten["riskLevel"] == "safe" and rewritten["decision"] == "auto"
    content = rewritten["message"]["content"]
    assert content.startswith("H" * PRUNE_HEAD_CHARS)
    assert content.endswith("T" * PRUNE_TAIL_CHARS)
    assert "M" not in content
    # Append-only: the original result is still in the log, byte for byte.
    assert original.data["message"]["content"] == _big_result(20_000)
    # Model-visible ⟺ logged: the surface (hence the next request) carries it.
    assert log.surface[2].data["message"]["content"] == content


@pytest.mark.asyncio
async def test_fresh_results_are_never_pruned_even_when_over_budget():
    """The working set is off limits: last turn's result reaches the summarizer
    (and, if kept, the model) byte for byte rather than pre-chewed."""
    log = _seed_log(result_turn=2)
    provider = RecordingProvider()
    callbacks = CollectingCallbacks()

    await _run(log, provider, window=2_000, callbacks=callbacks)

    assert _replace_events(log) == []
    assert callbacks.pruned == []
    assert len(provider.summarize_requests) == 1
    summarized = "\n".join(
        m.get("content") or "" for m in provider.summarize_requests[0]
        if isinstance(m.get("content"), str)
    )
    assert _big_result(20_000) in summarized
    assert PRUNED_MARKER not in summarized


@pytest.mark.asyncio
async def test_a_below_budget_conversation_is_never_pruned():
    """Pruning is pressure-gated, not opportunistic — an untouched prefix is what
    keeps the provider's prompt cache warm."""
    log = _seed_log()
    provider = RecordingProvider()
    callbacks = CollectingCallbacks()
    surface_before = list(log.surface)

    await _run(log, provider, window=100_000, callbacks=callbacks)

    assert _replace_events(log) == []
    assert callbacks.pruned == []
    assert [e.seq for e in log.surface][:len(surface_before)] == [
        e.seq for e in surface_before
    ]
    assert log.surface[2].data["message"]["content"] == _big_result(20_000)


# ── ordering vs. summarization ─────────────────────────────


@pytest.mark.asyncio
async def test_the_summarizer_runs_on_the_pruned_surface_not_the_full_one():
    """window 3000 -> limit 2400 tokens; three pruned results still exceed it, so
    compaction runs — and it must summarize what the model would have seen."""
    log = _seed_log(results=3)
    provider = RecordingProvider()

    await _run(log, provider, window=3_000)

    assert len(_replace_events(log)) == 3
    assert len(provider.summarize_requests) == 1
    summarized = "\n".join(
        m.get("content") or "" for m in provider.summarize_requests[0]
        if isinstance(m.get("content"), str)
    )
    assert PRUNED_MARKER in summarized
    assert "M" * 100 not in summarized
    # The summary is what stands in for the region afterwards.
    assert len(_compactions(log)) == 1
    assert "MIDDLE-SUMMARY" in "\n".join(_surface_texts(log))


@pytest.mark.asyncio
async def test_a_working_list_that_drifted_from_the_surface_is_left_alone():
    """The rewrite is positional, so it is applied only when the request list is
    exactly the log's surface plus the system prompt.

    A turn that follows an aborted one is the concrete drift: ``run()`` logs the
    interruption marker onto the surface but does not put it in the working list
    (that marker reaches the model one turn later, as history). Rewriting by
    position there would land the pruned text on an unrelated message, so the
    step must decline — the request may only change where it was told to.
    """
    log = _seed_log()
    log.append("turn/end", {"turn": 2, "reason": {"kind": "aborted"}})
    provider = RecordingProvider()
    callbacks = CollectingCallbacks()
    agent = _agent(log, provider, window=2_000)

    await agent.run("third", history=log.derive_messages(), callbacks=callbacks)

    # The marker is on the surface but, as before this feature, not in the
    # request — and the oversized result was NOT rewritten on top of that.
    assert _replace_events(log) == []
    assert callbacks.pruned == []
    assert any(
        m.get("content", "").startswith("[The previous turn was stopped")
        for m in log.derive_messages() if isinstance(m.get("content"), str)
    )
    assert not any(
        "previous turn was stopped" in (m.get("content") or "")
        for m in provider.requests[-1] if isinstance(m.get("content"), str)
    )
    # Nothing was silently dropped to compensate: the result reaches the
    # summarizer verbatim (the pre-existing route for relieving pressure).
    summarized = "\n".join(
        m.get("content") or "" for m in provider.summarize_requests[0]
        if isinstance(m.get("content"), str)
    )
    assert _big_result(20_000) in summarized


@pytest.mark.asyncio
async def test_prune_does_not_claim_a_compaction_to_the_frontends():
    """``compacted_this_turn`` drives injection invalidation and the "context
    compacted" notice — a prune folds nothing away and must not claim one."""
    log = _seed_log(results=3)
    provider = RecordingProvider()
    agent = _agent(log, provider, window=3_000)

    await agent.run("third", history=log.derive_messages())
    assert agent.compacted_this_turn is True  # a summary did run

    log2 = _seed_log()
    agent2 = _agent(log2, RecordingProvider(), window=2_000)
    await agent2.run("third", history=log2.derive_messages())
    assert agent2.compacted_this_turn is False


# ── the ContextViewer projection ───────────────────────────
# The desktop's context panel is rendered from `reconstruct_turn_contexts`, so
# anything it shows about a prune — and anything it must NOT show — is pinned
# here rather than in a renderer test (the desktop suite renders no components).


@pytest.mark.asyncio
async def test_the_context_viewer_is_told_which_results_were_pruned_and_what_they_were():
    """`reconstruct_turn_contexts` is what the desktop's context panel renders, and
    its Raw / Copy-raw view must stay the model's truth. So the step must keep
    showing the PRUNED text (not the original) and expose the original beside it
    as explicit metadata — never as the request content."""
    from cluxmate.core.session_log import reconstruct_turn_contexts

    log = _seed_log()
    await _run(log, RecordingProvider(), window=2_000)

    turns = {t["turn"]: t for t in reconstruct_turn_contexts(log.events)}
    # Only the turn that actually ran has step snapshots (the seeded turns have
    # no step/start), and it is the one that pruned.
    assert list(turns) == [3]
    step = turns[3]["steps"][0]
    assert len(step["pruned"]) == 1
    info = step["pruned"][0]
    assert info["call_id"] == "c0"
    # The message the panel (and the raw request) shows is the PRUNED one…
    assert "M" not in step["messages"][info["index"]]["content"]
    assert PRUNED_MARKER in step["messages"][info["index"]]["content"]
    # …and the original is available, measured, and kept out of `messages`.
    assert info["original"]["content"] == _big_result(20_000)
    assert info["original_chars"] == 20_000
    assert info["chars"] == len(step["messages"][info["index"]]["content"])
    assert info["chars"] < info["original_chars"]
    # The step's token estimate is the pruned one, so it still agrees with the
    # request the model actually got.
    assert step["tokens_estimate"] < 20_000 // 4

    # A step the pass never touched reports nothing, so the panel shows no badge.
    log2 = _seed_log()
    await _run(log2, RecordingProvider(), window=100_000)
    for turn in reconstruct_turn_contexts(log2.events):
        assert all(s["pruned"] == [] for s in turn["steps"])


@pytest.mark.asyncio
async def test_an_original_stays_reachable_after_a_compaction_absorbs_its_result():
    """A prune that does not clear the budget is followed by a summary in the same
    step, and the summary replaces the pruned nodes — so the originals would be
    lost from the panel unless the compaction carries them. `shadowed_pruned` is
    index-aligned with `shadowed` for exactly that reason."""
    from cluxmate.core.session_log import reconstruct_turn_contexts

    log = _seed_log(results=3)
    await _run(log, RecordingProvider(), window=3_000)

    assert len(_replace_events(log)) == 3          # the pass ran on all three
    step = reconstruct_turn_contexts(log.events)[-1]["steps"][0]
    # The compaction absorbed some pruned nodes and kept others in its preserved
    # tail, so both paths are visible at once — and every result is reachable
    # exactly once, whichever side of the cut it landed on.
    assert len(step["compactions"]) == 1
    aligned = step["compactions"][0]["shadowed_pruned"]
    assert len(aligned) == len(step["compactions"][0]["shadowed"])
    absorbed = [p for p in aligned if p is not None]
    assert absorbed and step["pruned"], "expected both a folded and a surviving prune"
    reachable = step["pruned"] + absorbed
    assert sorted(p["call_id"] for p in reachable) == ["c0", "c1", "c2"]
    for info in reachable:
        assert info["original"]["content"] == _big_result(20_000)
        assert info["original_chars"] == 20_000
        assert info["chars"] < info["original_chars"]
    # A shadowed message that was never pruned aligns to null rather than shifting.
    assert None in aligned
