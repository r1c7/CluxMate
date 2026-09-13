"""Subagent result header: status normalization + telemetry formatting."""

from cluxmate.tools.task import (
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
