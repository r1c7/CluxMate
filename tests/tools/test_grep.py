"""Tests for GrepTool."""

import tempfile
from pathlib import Path
import pytest
from cluxmate.tools.grep import GrepTool


@pytest.mark.asyncio
async def test_grep_finds_matches():
    tmpdir = tempfile.mkdtemp()
    try:
        Path(tmpdir, "a.txt").write_text("hello world\n")
        Path(tmpdir, "b.txt").write_text("hello there\ngoodbye\n")

        tool = GrepTool()
        result = await tool.execute(path=tmpdir, pattern="hello")
        assert "a.txt" in result
        assert "b.txt" in result
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.mark.asyncio
async def test_grep_no_matches():
    tmpdir = tempfile.mkdtemp()
    try:
        Path(tmpdir, "a.txt").write_text("hello\n")
        tool = GrepTool()
        result = await tool.execute(path=tmpdir, pattern="zzzz_not_there")
        assert "no matches" in result.lower() or result.strip() == ""
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.mark.asyncio
async def test_grep_nonexistent_path():
    tool = GrepTool()
    result = await tool.execute(path="/nonexistent", pattern="test")
    assert "not found" in result.lower()
