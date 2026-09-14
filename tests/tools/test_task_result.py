"""Subagent result header: status normalization + telemetry formatting."""

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
