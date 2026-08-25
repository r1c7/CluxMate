"""Tests for WriteFileTool and DeleteFileTool."""

import tempfile
from pathlib import Path

import pytest

from cluxmate.tools.write_file import WriteFileTool
from cluxmate.tools.delete_file import DeleteFileTool


@pytest.mark.asyncio
async def test_write_creates_new_file(tmp_path):
    tool = WriteFileTool(workdir=str(tmp_path))
    result = await tool.execute(path="sub/dir/new.txt", content="hello\nworld\n")
    assert "Created" in result
    f = tmp_path / "sub" / "dir" / "new.txt"
    assert f.read_text(encoding="utf-8") == "hello\nworld\n"


@pytest.mark.asyncio
async def test_write_overwrites_existing(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("old", encoding="utf-8")
    tool = WriteFileTool(workdir=str(tmp_path))
    result = await tool.execute(path="f.txt", content="new")
    assert "Overwrote" in result
    assert f.read_text(encoding="utf-8") == "new"


@pytest.mark.asyncio
async def test_write_handles_special_chars(tmp_path):
    # The whole point of a native write vs bash echo: quotes, redirection
    # metacharacters, unicode all round-trip verbatim.
    tricky = 'a "quoted" > redirect & <tag> 中文 \n\ttab'
    tool = WriteFileTool(workdir=str(tmp_path))
    await tool.execute(path="tricky.txt", content=tricky)
    assert (tmp_path / "tricky.txt").read_text(encoding="utf-8") == tricky


@pytest.mark.asyncio
async def test_write_new_file_uses_lf(tmp_path):
    # New files default to LF, not the platform's CRLF, so written code is
    # consistent across platforms.
    tool = WriteFileTool(workdir=str(tmp_path))
    await tool.execute(path="new.py", content="import os\nimport sys\n")
    assert (tmp_path / "new.py").read_bytes() == b"import os\nimport sys\n"


@pytest.mark.asyncio
async def test_write_preserves_crlf_on_overwrite(tmp_path):
    # Overwriting an existing CRLF file keeps CRLF.
    f = tmp_path / "crlf.txt"
    f.write_bytes(b"old\r\ntext\r\n")
    tool = WriteFileTool(workdir=str(tmp_path))
    await tool.execute(path="crlf.txt", content="new\nlines\n")
    assert f.read_bytes() == b"new\r\nlines\r\n"


@pytest.mark.asyncio
async def test_write_rejects_directory(tmp_path):
    (tmp_path / "d").mkdir()
    tool = WriteFileTool(workdir=str(tmp_path))
    result = await tool.execute(path="d", content="x")
    assert "Error" in result and "directory" in result


@pytest.mark.asyncio
async def test_write_risk_level():
    assert WriteFileTool().risk_level == "write"


@pytest.mark.asyncio
async def test_delete_removes_file(tmp_path):
    f = tmp_path / "gone.txt"
    f.write_text("bye", encoding="utf-8")
    tool = DeleteFileTool(workdir=str(tmp_path))
    result = await tool.execute(path="gone.txt")
    assert "Deleted" in result
    assert not f.exists()


@pytest.mark.asyncio
async def test_delete_missing_file(tmp_path):
    tool = DeleteFileTool(workdir=str(tmp_path))
    result = await tool.execute(path="nope.txt")
    assert "Error" in result and "not found" in result


@pytest.mark.asyncio
async def test_delete_rejects_directory(tmp_path):
    (tmp_path / "d").mkdir()
    tool = DeleteFileTool(workdir=str(tmp_path))
    result = await tool.execute(path="d")
    assert "Error" in result and "directory" in result
    assert (tmp_path / "d").exists()


@pytest.mark.asyncio
async def test_delete_risk_level():
    assert DeleteFileTool().risk_level == "dangerous"
