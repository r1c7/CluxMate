"""Tests for ListDirTool."""

import tempfile
from pathlib import Path
import pytest
from cluxmate.tools.list_dir import ListDirTool


@pytest.mark.asyncio
async def test_list_dir():
    tmpdir = tempfile.mkdtemp()
    try:
        Path(tmpdir, "file_a.txt").write_text("a")
        Path(tmpdir, "file_b.txt").write_text("b")
        Path(tmpdir, "sub").mkdir()

        tool = ListDirTool()
        result = await tool.execute(path=tmpdir)
        assert "file_a.txt" in result
        assert "file_b.txt" in result
        assert "sub" in result
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.mark.asyncio
async def test_list_dir_empty():
    tmpdir = tempfile.mkdtemp()
    try:
        tool = ListDirTool()
        result = await tool.execute(path=tmpdir)
        assert "empty" in result.lower()
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.mark.asyncio
async def test_list_dir_not_found():
    tool = ListDirTool()
    result = await tool.execute(path="/nonexistent_dir")
    assert "not found" in result.lower()
