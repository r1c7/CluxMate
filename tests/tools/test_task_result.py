"""Subagent result header: status normalization + telemetry formatting."""

import asyncio
from types import SimpleNamespace

import pytest

from cluxmate.core.session_log import SessionHeader, SessionLog
from cluxmate.core.subagents import BUILTIN_AGENT_TYPES
from cluxmate.tools.task import (
    TaskTool,
    _fmt_tokens,
    _normalize_status,
    _reported_status,
    _result_header,
)


def test_reported_status_reads_first_lines():
    assert _reported_status("**Status**: partial\nrest") == "partial"
    assert _reported_status("\n\n**Status**: SUCCESS") == "success"
    assert _reported_status("blah\n**Status**: blocked") == "blocked"


def test_reported_status_missing_is_none():
    assert _reported_status("I did the thing") is None
    assert _reported_status("") is None
    assert _reported_status("**Status**: maybe") is None


def test_normalize_downgrades_claimed_success_on_abnormal_end():
    assert _normalize_status("success", "max-turns") == "partial"
    assert _normalize_status(None, "error") == "partial"


def test_normalize_keeps_a_childs_own_failure_claim():
    assert _normalize_status("failed", "max-turns") == "failed"
    assert _normalize_status("blocked", None) == "blocked"


def test_normalize_unknown_when_quiet_and_clean():
    assert _normalize_status(None, "completed") == "unknown"
    assert _normalize_status("success", "completed") == "success"


def test_fmt_tokens():
    assert _fmt_tokens(999) == "999"
    assert _fmt_tokens(3400) == "3.4k"


def test_header_minimal():
    h = _result_header("explore", "success", turns=7, max_turns=50, out_tokens=3400, elapsed_ms=12_400)
    assert h == "[subagent: explore | status=success | turns=7/50 | out=3.4k tok | 12s]"


def test_header_abnormal_includes_end_and_wait():
    h = _result_header("general-purpose", "partial", end_kind="max-turns", turns=50, max_turns=50, elapsed_ms=96_000, queued_ms=1200)
    assert h == (
        "[subagent: general-purpose | status=partial | end=max-turns | turns=50/50 | 96s | wait=1.2s]"
    )


def test_header_omits_wait_when_not_queued():
    h = _result_header("explore", "unknown", turns=1, max_turns=50, queued_ms=30)
    assert "wait=" not in h


def test_header_names_an_unbacked_claim():
    h = _result_header(
        "general-purpose", "partial", turns=5, max_turns=50, note="unbacked=claim"
    )
    assert h == (
        "[subagent: general-purpose | status=partial | turns=5/50 | unbacked=claim]"
    )


# ------------------------------------------------- audit evidence at the boundary


def _child(reason: dict | None = None) -> SimpleNamespace:
    log = SessionLog.create(SessionHeader(id="c1", createdAt=0, apiType="openai"))
    if reason is not None:
        log.append("turn/end", {"turn": 1, "reason": reason})
    return SimpleNamespace(session_log=log, max_turns=50)


def test_child_audit_unbacked_reads_the_childs_own_turn_end():
    assert TaskTool._child_audit_unbacked(_child({"kind": "completed"})) is False
    assert (
        TaskTool._child_audit_unbacked(
            _child(
                {
                    "kind": "completed",
                    "completion_audit": {"reminders": 1, "unbacked": True},
                }
            )
        )
        is True
    )
    # No log / no turn/end / no audit record → never fabricate a verdict.
    assert TaskTool._child_audit_unbacked(_child()) is False
    assert TaskTool._child_audit_unbacked(SimpleNamespace(session_log=None)) is False


def test_child_end_kind_still_reads_the_same_event():
    assert TaskTool._child_end_kind(_child({"kind": "max-turns"})) == "max-turns"
    assert TaskTool._child_end_kind(_child()) is None


def test_normalize_downgrades_a_claim_the_childs_own_audit_refuted():
    assert _normalize_status("success", "completed", True) == "partial"
    assert _normalize_status(None, "completed", True) == "partial"
    # The child's own worse admission stays (more specific than ours).
    assert _normalize_status("failed", "completed", True) == "failed"
    # A clean turn is untouched.
    assert _normalize_status("success", "completed", False) == "success"


class _StubChild:
    """Subagent stand-in carrying the pieces TaskTool reads back."""

    def __init__(self, text: str, reason: dict):
        self.max_turns = 50
        self.session_log = SessionLog.create(
            SessionHeader(id="c1", createdAt=0, apiType="openai")
        )
        self.session_log.append("turn/end", {"turn": 1, "reason": reason})
        self._text = text

    async def run(self, prompt, history=None, callbacks=None):
        return SimpleNamespace(text=self._text, out_tokens=0, turns=1, cache_usage={})


class _StubBuilder:
    """AgentBuilder stand-in carrying just what TaskTool.execute touches."""

    _tracker = None
    _agent_id = "root"
    _depth = 0
    _log_store = None
    cwd = None

    def __init__(self, child: _StubChild):
        self._child = child

    def allowed_subagent_slugs(self):
        return ["general-purpose"]

    def agent_type(self, slug):
        return BUILTIN_AGENT_TYPES[slug]

    def _scheduler_for_loop(self):
        import asyncio

        from cluxmate.core.subagent_scheduler import SubagentScheduler

        return SubagentScheduler(asyncio.get_running_loop())

    def build_child(self, subagent_type, description, child_id, **kwargs):
        return self._child


@pytest.mark.asyncio
async def test_unbacked_child_is_reported_partial_not_success():
    """The child's **Status** line says success, but its own committed reply
    failed its own completion audit — the engine's record wins at the
    delegation boundary."""
    builder = _StubBuilder(
        _StubChild(
            "**Status**: success\nI fixed utils.py.",
            {
                "kind": "completed",
                "completion_audit": {"reminders": 1, "unbacked": True},
            },
        )
    )
    out = await TaskTool(builder).execute(
        subagent_type="general-purpose", description="d", prompt="p",
    )
    assert out.startswith("[subagent: general-purpose | status=partial")
    assert "unbacked=claim" in out
    assert out.endswith("**Status**: success\nI fixed utils.py.")


@pytest.mark.asyncio
async def test_backed_child_keeps_its_own_success():
    builder = _StubBuilder(
        _StubChild("**Status**: success\nWrote utils.py.", {"kind": "completed"})
    )
    out = await TaskTool(builder).execute(
        subagent_type="general-purpose", description="d", prompt="p",
    )
    assert out.startswith("[subagent: general-purpose | status=success")
    assert "note=" not in out


# ------------------------------------------------- token accounting at the boundary


class _UsageTracker:
    """Stands in for the JSON-RPC tracker; records every agent_end report."""

    def __init__(self):
        self.ends: list[dict] = []

    async def on_agent_start(self, *args, **kwargs):
        pass

    async def on_agent_end(self, agent_id, status, result, **kw):
        self.ends.append({"agent_id": agent_id, "status": status, "result": result, **kw})

    def scoped(self, agent_id, auto_approve=True):
        return None


def _tracked_builder(tracker, child):
    builder = _StubBuilder(child)
    builder._tracker = tracker
    return builder


class _RaisingChild:
    """A child whose turn raised after its LLM calls were already billed."""

    def __init__(self, usage: dict):
        self.max_turns = 50
        self.session_log = SessionLog.create(
            SessionHeader(id="c1", createdAt=0, apiType="openai")
        )
        self.session_log.append("turn/end", {"turn": 1, "reason": {"kind": "error"}})
        self.turn_usage = usage

    async def run(self, prompt, history=None, callbacks=None):
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_failed_child_reports_the_tokens_it_spent_before_dying():
    """A raised child has no AgentResult to report through, so agent_end used to
    carry 0/0 — the live subagent tree then read LOWER than the tree the desktop
    rebuilds from the same child JSONL (which folds every assistant/message)."""
    tracker = _UsageTracker()
    child = _RaisingChild({"input_tokens": 4321, "output_tokens": 210})
    out = await TaskTool(_tracked_builder(tracker, child)).execute(
        subagent_type="general-purpose", description="d", prompt="p",
    )

    assert out.startswith("[subagent: general-purpose | status=failed")
    assert len(tracker.ends) == 1
    end = tracker.ends[0]
    assert end["agent_id"]
    assert end["status"] == "error"
    assert end["input_tokens"] == 4321
    assert end["output_tokens"] == 210


@pytest.mark.asyncio
async def test_failed_child_without_a_usage_record_reports_zero():
    """No fabricated numbers: a child that never reached the model (build
    failure, no turn_usage) reports 0/0 instead of raising in the error path."""
    tracker = _UsageTracker()
    child = _RaisingChild({"input_tokens": 4321, "output_tokens": 210})
    del child.turn_usage
    out = await TaskTool(_tracked_builder(tracker, child)).execute(
        subagent_type="general-purpose", description="d", prompt="p",
    )

    assert out.startswith("[subagent: general-purpose | status=failed")
    assert tracker.ends[0]["input_tokens"] == 0
    assert tracker.ends[0]["output_tokens"] == 0


class _CancellingChild:
    """A child cancelled mid-turn (Stop), i.e. raising a BaseException."""

    def __init__(self, usage: dict):
        self.max_turns = 50
        self.session_log = SessionLog.create(
            SessionHeader(id="c1", createdAt=0, apiType="openai")
        )
        self.session_log.append("turn/end", {"turn": 1, "reason": {"kind": "aborted"}})
        self.turn_usage = usage

    async def run(self, prompt, history=None, callbacks=None):
        raise asyncio.CancelledError()


class _ExplodingTracker(_UsageTracker):
    """A tracker whose report itself fails (the turn is being torn down)."""

    async def on_agent_end(self, agent_id, status, result, **kw):
        self.ends.append({"agent_id": agent_id, "status": status, **kw})
        raise RuntimeError("stdout already torn down")


@pytest.mark.asyncio
async def test_cancelled_child_reports_its_spent_tokens_and_still_cancels():
    """Stop mid-subagent: CancelledError is a BaseException, so the error branch
    never ran and the node stayed "running" with 0 tokens while the child's own
    JSONL already held them — the live tree read lower than the replay fold."""
    tracker = _UsageTracker()
    child = _CancellingChild({"input_tokens": 8888, "output_tokens": 77})

    with pytest.raises(asyncio.CancelledError):
        await TaskTool(_tracked_builder(tracker, child)).execute(
            subagent_type="general-purpose", description="d", prompt="p",
        )

    assert len(tracker.ends) == 1
    end = tracker.ends[0]
    assert end["status"] == "error"       # matches the replay classification
    assert end["input_tokens"] == 8888
    assert end["output_tokens"] == 77
    # The node's error text says WHY it stopped (the inspector renders this in
    # place of "no activity" when the node has no blocks).
    assert "cancelled" in end["result"]


@pytest.mark.asyncio
async def test_a_failing_cancellation_report_does_not_replace_the_cancel():
    """Reporting is best-effort: if it blows up, the original CancelledError must
    still be what the parent loop sees (never the reporting error)."""
    tracker = _ExplodingTracker()
    child = _CancellingChild({"input_tokens": 5, "output_tokens": 1})

    with pytest.raises(asyncio.CancelledError):
        await TaskTool(_tracked_builder(tracker, child)).execute(
            subagent_type="general-purpose", description="d", prompt="p",
        )

    assert tracker.ends[0]["input_tokens"] == 5
