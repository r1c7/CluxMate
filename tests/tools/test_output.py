"""Tests for tool-output bounding: head/tail truncation + spill (P0-1/P0-2)."""

import os
import re
import time
from pathlib import Path

import pytest

from cluxmate.tools._output import (
    ERROR_TAIL_FRACTION,
    SPILL_DIR_NAME,
    SPILL_RETENTION_DAYS,
    bound_output,
    head_tail,
)
from cluxmate.tools.base import MAX_OUTPUT_CHARS, BaseTool

FULL_MARKER = re.compile(r"\[\.\.\. \d+ characters omitted \.\.\.\]")


class _EchoTool(BaseTool):
    """Minimal tool returning a fixed string (optionally with a workdir)."""

    def __init__(self, text: str, workdir: str | None = None, error: str | None = None):
        self._text = text
        self._error = error
        if workdir is not None:
            self._workdir = workdir

    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "echo"

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> str:
        if self._error:
            raise RuntimeError(self._error)
        return self._text


def _big_text() -> str:
    """1000 head markers, a large middle, 1000 tail markers — all line-broken."""
    return "H" * 999 + "\n" + ("m" * 79 + "\n") * 200 + "T" * 999 + "\n"


def _over_cap_text() -> str:
    """A string comfortably above MAX_OUTPUT_CHARS (run_safe only bounds there)."""
    return _big_text() * 4


def _spill_files(cwd: Path) -> list[Path]:
    directory = cwd / ".cluxmate" / SPILL_DIR_NAME
    if not directory.is_dir():
        return []
    return sorted(directory.iterdir())


# -- head/tail truncation (P0-2) -------------------------------------------


def test_short_text_is_untouched():
    assert bound_output("hello", cwd=None, tool_name="bash", max_chars=10) == "hello"


def test_exactly_at_cap_is_untouched():
    text = "x" * MAX_OUTPUT_CHARS
    assert bound_output(text, cwd=None, tool_name="bash", max_chars=MAX_OUTPUT_CHARS) == text


def test_both_ends_survive_and_middle_is_marked():
    text = _big_text()
    out = bound_output(text, cwd=None, tool_name="bash", max_chars=600)

    assert out.startswith("H" * 100)
    assert out.endswith("\n")
    assert "T" * 100 in out
    assert "characters omitted" in out
    assert len(out) <= 600


def test_omitted_count_matches_what_was_dropped():
    text = _big_text()
    out = bound_output(text, cwd=None, tool_name="bash", max_chars=1000)
    marker = FULL_MARKER.search(out).group(0)
    omitted = int(re.search(r"\d+", marker).group(0))
    # Exactly the middle vanished: kept = len(text) - omitted, and the kept
    # characters are the result minus the marker and the two "\n\n" joins.
    assert len(text) - omitted == len(out) - len(marker) - 4


def test_symmetric_split_without_errors():
    text = _big_text()
    head, tail, omitted = head_tail(text, 1000)
    assert omitted == len(text) - len(head) - len(tail)
    # 50/50 modulo the bounded line-boundary trim.
    assert abs(len(head) - len(tail)) <= 200


def test_error_in_tail_shifts_budget_toward_tail():
    text = _big_text() + "Traceback (most recent call last):\nValueError: boom\n"
    head, tail, _ = head_tail(text, 1000)
    assert len(tail) > len(head)
    assert tail.rstrip().endswith("ValueError: boom")
    # Tail gets ~70% of the budget; the line-boundary trim costs a little.
    assert len(tail) >= int(1000 * ERROR_TAIL_FRACTION) - 200


def test_error_pattern_only_counts_near_the_tail():
    # An "error" far from the end must NOT trigger the tail bias.
    text = "error: something\n" + "x" * 5000 + "\n" + "y" * 5000
    head, tail, _ = head_tail(text, 1000)
    assert abs(len(head) - len(tail)) <= 200


def test_previews_cut_on_line_boundaries():
    text = "".join(f"line {i}\n" for i in range(1000))
    head, tail, _ = head_tail(text, 400)
    assert head and tail
    assert text[len(head)] == "\n"  # head stops at a line end
    assert text[len(text) - len(tail) - 1] == "\n"  # tail starts at a line start
    assert head.splitlines()[-1].startswith("line ")
    assert tail.splitlines()[0].startswith("line ")


def test_one_huge_line_keeps_its_budget():
    # A single enormous line must not be trimmed down to nothing.
    text = "x" * 5000
    head, tail, omitted = head_tail(text, 1000)
    assert len(head) == 500
    assert len(tail) == 500
    assert omitted == 4000


def test_budget_of_zero_drops_everything():
    assert head_tail("abc", 0) == ("", "", 3)


# -- spill (P0-1) ----------------------------------------------------------


def test_spill_writes_full_text_and_points_at_it(tmp_path):
    text = _big_text()
    out = bound_output(text, cwd=str(tmp_path), tool_name="bash", max_chars=600)

    files = _spill_files(tmp_path)
    assert len(files) == 1
    assert files[0].read_bytes() == text.encode("utf-8")
    assert files[0].name.startswith("bash-")
    assert str(files[0]) in out
    assert "read_file" in out
    assert out.startswith("H" * 100)
    assert len(out) <= 600


def test_spill_skips_read_file(tmp_path):
    out = bound_output(_big_text(), cwd=str(tmp_path), tool_name="read_file", max_chars=600)
    assert _spill_files(tmp_path) == []
    assert "characters omitted ..." in out


def test_spill_needs_a_cwd(tmp_path):
    out = bound_output(_big_text(), cwd=None, tool_name="bash", max_chars=600)
    assert _spill_files(tmp_path) == []
    assert "characters omitted ..." in out


def test_spill_failure_degrades_to_inline(monkeypatch, tmp_path):
    def boom(self, data):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_bytes", boom)
    out = bound_output(_big_text(), cwd=str(tmp_path), tool_name="bash", max_chars=600)
    assert "characters omitted ..." in out
    assert "saved to" not in out


def test_spill_is_bounded_by_the_cap(tmp_path):
    out = bound_output(_big_text(), cwd=str(tmp_path), tool_name="bash", max_chars=800)
    assert len(out) <= 800


def test_retention_sweep_removes_only_expired_files(tmp_path):
    directory = tmp_path / ".cluxmate" / SPILL_DIR_NAME
    directory.mkdir(parents=True)
    old = directory / "bash-old.txt"
    old.write_text("stale")
    stale_at = time.time() - (SPILL_RETENTION_DAYS + 1) * 86_400
    os.utime(old, (stale_at, stale_at))
    fresh = directory / "bash-fresh.txt"
    fresh.write_text("recent")

    bound_output(_big_text(), cwd=str(tmp_path), tool_name="grep", max_chars=600)

    assert not old.exists()
    assert fresh.exists()


def test_spill_filename_has_no_path_separators(tmp_path):
    out = bound_output(_big_text(), cwd=str(tmp_path), tool_name="mcp__srv__weird/tool", max_chars=600)
    files = _spill_files(tmp_path)
    assert len(files) == 1
    assert files[0].name.startswith("mcp__srv__weird_tool-")
    assert str(files[0]) in out


@pytest.mark.asyncio
async def test_spilled_text_is_recoverable_with_read_file(tmp_path):
    """The notice's promise: the omitted middle is retrievable with read_file."""
    from cluxmate.tools.read_file import ReadFileTool

    text = "".join(f"line {i:06d}\n" for i in range(8000))
    out = bound_output(text, cwd=str(tmp_path), tool_name="bash", max_chars=1000)
    path = _spill_files(tmp_path)[0]
    assert str(path) in out
    assert "line 004000" not in out  # the middle is what got dropped

    read = await ReadFileTool(workdir=str(tmp_path)).execute(
        path=str(path), offset=4001, limit=3
    )
    assert "line 004000" in read
    assert "line 004002" in read


# -- run_safe wiring -------------------------------------------------------


@pytest.mark.asyncio
async def test_run_safe_spills_when_workdir_is_known(tmp_path):
    text = _over_cap_text()
    tool = _EchoTool(text, workdir=str(tmp_path))
    result = await tool.run_safe("call-1")
    assert result.is_error is False
    files = _spill_files(tmp_path)
    assert len(files) == 1
    assert files[0].read_bytes() == text.encode("utf-8")
    assert str(files[0]) in result.content
    assert len(result.content) <= MAX_OUTPUT_CHARS


@pytest.mark.asyncio
async def test_run_safe_truncates_without_workdir():
    tool = _EchoTool(_over_cap_text())
    result = await tool.run_safe("call-1")
    assert result.is_error is False
    assert "characters omitted" in result.content
    assert len(result.content) <= MAX_OUTPUT_CHARS


@pytest.mark.asyncio
async def test_run_safe_keeps_small_output_verbatim():
    tool = _EchoTool("tiny", workdir=None)
    result = await tool.run_safe("call-1")
    assert result.content == "tiny"
    assert result.is_error is False


@pytest.mark.asyncio
async def test_run_safe_error_result_is_unchanged_by_bounding():
    tool = _EchoTool("irrelevant", workdir=None, error="boom")
    result = await tool.run_safe("call-1")
    assert result.is_error is True
    assert result.content == "Error: boom"
