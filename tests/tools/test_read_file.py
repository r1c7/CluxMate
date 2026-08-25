"""Tests for ReadFileTool."""

import tempfile
import pytest
from pathlib import Path
from cluxmate.tools.read_file import ReadFileTool


@pytest.mark.asyncio
async def test_read_file():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("line1\nline2\nline3\n")
        path = f.name

    try:
        tool = ReadFileTool()
        result = await tool.execute(path=path)
        assert "line1" in result
        assert "line2" in result
        assert "line3" in result
    finally:
        Path(path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_read_file_with_offset_limit():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("a\nb\nc\nd\ne\n")
        path = f.name

    try:
        tool = ReadFileTool()
        result = await tool.execute(path=path, offset=2, limit=2)
        lines = result.strip().split("\n")
        assert len([l for l in lines if l]) == 2
    finally:
        Path(path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_read_file_not_found():
    tool = ReadFileTool()
    result = await tool.execute(path="/nonexistent/file.txt")
    assert "not found" in result.lower()
