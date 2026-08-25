"""Tests for SearchReplaceTool."""

import tempfile
from pathlib import Path
import pytest
from cluxmate.tools.search_replace import SearchReplaceTool


@pytest.mark.asyncio
async def test_search_replace_single():
    path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("hello world\n")
            path = f.name

        tool = SearchReplaceTool()
        result = await tool.execute(
            path=path,
            old_string="hello world",
            new_string="goodbye world",
        )
        assert "Replaced" in result
        content = Path(path).read_text()
        assert content == "goodbye world\n"
    finally:
        if path:
            Path(path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_search_replace_not_found():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("hello\n")
        path = f.name
    try:
        tool = SearchReplaceTool()
        result = await tool.execute(
            path=path,
            old_string="not in file",
            new_string="replacement",
        )
        assert "not found" in result.lower()
    finally:
        Path(path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_search_replace_all():
    path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("foo foo foo\n")
            path = f.name

        tool = SearchReplaceTool()
        result = await tool.execute(
            path=path,
            old_string="foo",
            new_string="bar",
            replace_all=True,
        )
        assert "3 occurrence" in result
        assert Path(path).read_text() == "bar bar bar\n"
    finally:
        if path:
            Path(path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_preserves_lf_newlines(tmp_path):
    # Editing one line of an LF file must NOT flip every ending to CRLF.
    f = tmp_path / "lf.txt"
    f.write_bytes(b"line1\nline2\nline3\n")
    tool = SearchReplaceTool(workdir=str(tmp_path))
    await tool.execute(path="lf.txt", old_string="line2", new_string="LINE2")
    assert f.read_bytes() == b"line1\nLINE2\nline3\n"


@pytest.mark.asyncio
async def test_preserves_crlf_newlines(tmp_path):
    # A CRLF file stays CRLF; the model matches against LF-normalized content.
    f = tmp_path / "crlf.txt"
    f.write_bytes(b"a\r\nb\r\nc\r\n")
    tool = SearchReplaceTool(workdir=str(tmp_path))
    result = await tool.execute(path="crlf.txt", old_string="b", new_string="B")
    assert "Replaced" in result
    assert f.read_bytes() == b"a\r\nB\r\nc\r\n"


@pytest.mark.asyncio
async def test_non_unique_match_errors(tmp_path):
    # Multiple matches without replace_all must error, not silently edit the
    # first occurrence.
    f = tmp_path / "dup.txt"
    f.write_bytes(b"x = 1\ny = 1\nz = 1\n")
    tool = SearchReplaceTool(workdir=str(tmp_path))
    result = await tool.execute(path="dup.txt", old_string="= 1", new_string="= 2")
    assert "not unique" in result.lower()
    assert "3 times" in result
    # File must be untouched.
    assert f.read_bytes() == b"x = 1\ny = 1\nz = 1\n"
