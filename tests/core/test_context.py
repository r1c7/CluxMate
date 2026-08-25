"""Tests for core/context.py — token estimation + hybrid compaction."""

import pytest

from cluxmate.core.context import (
    compact,
    estimate_tokens,
    _is_tool_result,
    _split_head,
)
from cluxmate.core.providers.base import LLMResponse


class SummarizeProvider:
    """Provider stub that records summarize calls and returns a short summary."""

    def __init__(self, summary: str = "SUMMARY", fail: bool = False):
        self.summary = summary
        self.fail = fail
        self.calls = 0

    async def chat(self, messages, tools):
        self.calls += 1
        if self.fail:
            raise RuntimeError("summarize boom")
        return LLMResponse(text=self.summary, stop_reason="end_turn")


def _big(n: int) -> str:
    return "x" * n


# ── estimate_tokens ────────────────────────────────────────


def test_estimate_str_content():
    msgs = [{"role": "user", "content": "a" * 40}]
    assert estimate_tokens(msgs) == 10  # 40 chars // 4


def test_estimate_anthropic_block_list():
    msgs = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "a" * 20},
            {"type": "tool_result", "tool_use_id": "t1", "content": "b" * 20},
        ],
    }]
    assert estimate_tokens(msgs) == 10  # (20 + 20) // 4


def test_estimate_openai_tool_calls_args():
    msgs = [{
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "t1", "type": "function",
             "function": {"name": "read", "arguments": '{"path":"' + "y" * 32 + '"}'}},
        ],
    }]
    # arguments string is 32 + len('{"path":""}') = 32 + 11 = 43 chars.
    assert estimate_tokens(msgs) == 43 // 4


# ── _is_tool_result ────────────────────────────────────────


def test_is_tool_result_openai():
    assert _is_tool_result({"role": "tool", "tool_call_id": "t", "content": "r"})


def test_is_tool_result_anthropic():
    assert _is_tool_result({
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "t", "content": "r"}],
    })


def test_plain_user_is_not_tool_result():
    assert not _is_tool_result({"role": "user", "content": "hello"})


# ── _split_head: environment-injection anchor skipping ─────


def test_split_head_skips_environment_injections():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "[Project memory]\nconventions"},
        {"role": "user", "content": "[Available skills]\n- foo"},
        {"role": "user", "content": "the real task"},
        {"role": "assistant", "content": "reply"},
    ]
    sources = [None, "memory", "skill", "human", None]
    head, head_end = _split_head(msgs, sources)
    assert [m["content"] for m in head] == [
        "sys", "[Project memory]\nconventions", "[Available skills]\n- foo", "the real task",
    ]
    assert head_end == 4


def test_split_head_without_sources_uses_first_user():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
    ]
    head, head_end = _split_head(msgs)
    assert [m["content"] for m in head] == ["sys", "first"]
    assert head_end == 2


# ── compact: under threshold ───────────────────────────────


@pytest.mark.asyncio
async def test_under_threshold_noop():
    provider = SummarizeProvider()
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    out, did, edit = await compact(msgs, window=128_000, provider=provider)
    assert did is False
    assert out is msgs
    assert edit is None
    assert provider.calls == 0


# ── compact: summarize path ────────────────────────────────


@pytest.mark.asyncio
async def test_giant_tool_result_summarized():
    # Window tiny so we go over; one giant OLD tool result in the middle.
    provider = SummarizeProvider(summary="old tool output collapsed")
    window = 4_000  # limit = 3200 tokens
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "original task"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": _big(80_000)},  # ~20k tok
        {"role": "user", "content": "recent followup"},
    ]
    assert estimate_tokens(msgs) > int(0.8 * window)
    out, did, edit = await compact(msgs, window=window, provider=provider)
    assert did is True
    assert provider.calls == 1  # single region-replace: always one summarize call
    # System + original task preserved verbatim.
    assert out[0]["content"] == "sys"
    assert out[1]["content"] == "original task"
    assert estimate_tokens(out) <= int(0.8 * window)
    # edit replaces messages[2:4] (assistant + tool result) with one summary.
    start, end, replacement = edit
    assert (start, end) == (2, 4)
    assert len(replacement) == 1
    assert "old tool output collapsed" in replacement[0]["content"]


@pytest.mark.asyncio
async def test_summarize_path_called_once():
    provider = SummarizeProvider(summary="a compressed recap")
    window = 4_000
    # Many old exchanges whose non-tool text is large enough that a single
    # middle region exceeds budget → forces summarize.
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "original task"},
    ]
    for i in range(6):
        msgs.append({"role": "assistant", "content": _big(6_000)})  # large TEXT
        msgs.append({"role": "user", "content": f"turn {i}"})
    msgs.append({"role": "user", "content": "the latest question"})

    out, did, edit = await compact(msgs, window=window, provider=provider)
    assert did is True
    assert provider.calls == 1
    # Exactly one synthetic summary message inserted after the head.
    summaries = [m for m in out if isinstance(m.get("content"), str)
                 and m["content"].startswith("[Earlier conversation summary]")]
    assert len(summaries) == 1
    assert "a compressed recap" in summaries[0]["content"]
    assert out[0]["content"] == "sys"
    assert out[1]["content"] == "original task"
    # edit reconstructs the output exactly: out == head + replacement + tail.
    start, end, replacement = edit
    assert out == msgs[:start] + replacement + msgs[end:]


# ── compact: boundary safety ───────────────────────────────


@pytest.mark.asyncio
async def test_tail_never_starts_with_orphan_tool_result():
    provider = SummarizeProvider()
    window = 4_000
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
    ]
    # Alternating assistant-tool_call / tool-result pairs, all large.
    for i in range(8):
        msgs.append({"role": "assistant", "content": None,
                     "tool_calls": [{"id": f"t{i}", "type": "function",
                                     "function": {"name": "read", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"t{i}", "content": _big(20_000)})

    out, did, edit = await compact(msgs, window=window, provider=provider)
    assert did is True
    # No message that is a tool-result may immediately follow the head/summary
    # without its assistant tool_call present before it. Simplest invariant: the
    # first message after the preserved head is not an orphan tool-result.
    head, head_end = _split_head(msgs)
    # In the output, find where the tail begins (first non-head, non-summary msg)
    # and assert it isn't a bare tool-result.
    for m in out[len(head):]:
        if isinstance(m.get("content"), str) and (
            m["content"].startswith("[Earlier conversation")
        ):
            continue
        assert not _is_tool_result(m), "tail must not start with an orphan tool_result"
        break


# ── compact: summarize failure fallback ────────────────────


@pytest.mark.asyncio
async def test_summarize_failure_falls_back_to_note():
    provider = SummarizeProvider(fail=True)
    window = 4_000
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "original task"},
    ]
    for i in range(6):
        msgs.append({"role": "assistant", "content": _big(6_000)})
        msgs.append({"role": "user", "content": f"turn {i}"})
    msgs.append({"role": "user", "content": "latest"})

    out, did, edit = await compact(msgs, window=window, provider=provider)
    assert did is True
    assert provider.calls == 1  # attempted once, then fell back
    # Did not raise, and got under budget.
    assert estimate_tokens(out) <= int(0.8 * window)
    # A truncation note is present instead of a real summary.
    assert any(isinstance(m.get("content"), str)
               and m["content"].startswith("[Earlier conversation truncated")
               for m in out)
    # The fallback is still a single region-replace.
    start, end, replacement = edit
    assert out == msgs[:start] + replacement + msgs[end:]
