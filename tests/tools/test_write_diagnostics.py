"""Tests for the post-write LSP diagnostics wiring in the file tools (P0-5).

The tools are exercised with a stub manager, so these pin the wiring contract:
which paths get queried, whether the block reaches the result, and — most
importantly — that a broken or absent language server can never alter a write's
own result.
"""

from pathlib import Path

import pytest

from cluxmate.tools._diagnostics import HEADER, MAX_FILES
from cluxmate.tools.multi_edit import MultiEditTool
from cluxmate.tools.multi_write import MultiWriteTool
from cluxmate.tools.search_replace import SearchReplaceTool
from cluxmate.tools.write_file import WriteFileTool

BLOCK = '<diagnostics file="a.py">\n1:1 [error] boom\n</diagnostics>'


class StubLSP:
    """Records queried paths; returns a canned block (or raises)."""

    def __init__(self, block: str = BLOCK, raises: bool = False):
        self.block = block
        self.raises = raises
        self.queried: list[str] = []

    def auto_diagnostics(self, path: str) -> str:
        self.queried.append(path)
        if self.raises:
            raise RuntimeError("language server died")
        return self.block


@pytest.mark.asyncio
async def test_write_file_appends_diagnostics(tmp_path):
    lsp = StubLSP()
    tool = WriteFileTool(workdir=str(tmp_path), lsp=lsp)
    out = await tool.execute(path="a.py", content="x = 1\n")

    assert out.startswith("Created ")
    assert out.endswith(f"{HEADER}\n{BLOCK}")
    # Queried with the resolved absolute path, not the caller's relative one.
    assert lsp.queried == [str(tmp_path / "a.py")]


@pytest.mark.asyncio
async def test_write_file_without_manager_is_unchanged(tmp_path):
    tool = WriteFileTool(workdir=str(tmp_path))
    out = await tool.execute(path="a.py", content="x = 1\n")
    assert out == f"Created {tmp_path / 'a.py'} (2 line(s), 6 chars)"
    assert "LSP" not in out


@pytest.mark.asyncio
async def test_write_file_survives_a_broken_language_server(tmp_path):
    tool = WriteFileTool(workdir=str(tmp_path), lsp=StubLSP(raises=True))
    out = await tool.execute(path="a.py", content="x = 1\n")
    assert out.startswith("Created ")
    assert "LSP" not in out
    assert "Error" not in out


@pytest.mark.asyncio
async def test_write_file_clean_result_has_no_trailing_block(tmp_path):
    tool = WriteFileTool(workdir=str(tmp_path), lsp=StubLSP(block=""))
    out = await tool.execute(path="a.py", content="x = 1\n")
    assert out == f"Created {tmp_path / 'a.py'} (2 line(s), 6 chars)"
    assert not out.endswith("\n")


@pytest.mark.asyncio
async def test_search_replace_appends_diagnostics(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    lsp = StubLSP()
    tool = SearchReplaceTool(workdir=str(tmp_path), lsp=lsp)
    out = await tool.execute(path="a.py", old_string="x = 1", new_string="x = 2")

    assert "Replaced 1 occurrence(s)" in out
    assert HEADER in out and BLOCK in out
    assert lsp.queried == [str(tmp_path / "a.py")]


@pytest.mark.asyncio
async def test_failed_edit_is_not_queried(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    lsp = StubLSP()
    tool = SearchReplaceTool(workdir=str(tmp_path), lsp=lsp)
    out = await tool.execute(path="a.py", old_string="NOT PRESENT", new_string="y")

    assert out.startswith("Error:")
    assert lsp.queried == []


@pytest.mark.asyncio
async def test_multi_edit_reports_only_written_files(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("y = 1\n", encoding="utf-8")
    lsp = StubLSP()
    tool = MultiEditTool(workdir=str(tmp_path), lsp=lsp)
    out = await tool.execute(edits=[
        {"path": "a.py", "old_string": "x = 1", "new_string": "x = 2"},
        {"path": "b.py", "old_string": "MISSING", "new_string": "z"},
    ])

    assert "Applied 1/2 edits:" in out
    assert HEADER in out
    assert lsp.queried == [str(tmp_path / "a.py")]


@pytest.mark.asyncio
async def test_multi_write_is_capped_and_deduped(tmp_path):
    lsp = StubLSP()
    tool = MultiWriteTool(workdir=str(tmp_path), lsp=lsp)
    files = [{"path": f"f{i}.py", "content": "x = 1\n"} for i in range(MAX_FILES + 2)]
    # The last entry repeats the first file: dedupe must not spend a slot on it.
    files.append({"path": "f0.py", "content": "x = 1\n"})
    out = await tool.execute(files=files)

    assert out.startswith(f"Wrote {MAX_FILES + 3}/{MAX_FILES + 3} files:")
    assert lsp.queried == [str(tmp_path / f"f{i}.py") for i in range(MAX_FILES)]
    assert out.count("<diagnostics") == MAX_FILES


@pytest.mark.asyncio
async def test_multi_write_errors_do_not_stop_the_report(tmp_path):
    """One broken file must not swallow the others' diagnostics."""
    lsp = StubLSP()
    tool = MultiWriteTool(workdir=str(tmp_path), lsp=lsp)
    (tmp_path / "dir").mkdir()
    out = await tool.execute(files=[
        {"path": "dir", "content": "x"},          # error: path is a directory
        {"path": "ok.py", "content": "x = 1\n"},
    ])

    assert "✗ path is a directory: dir" in out
    assert HEADER in out
    assert lsp.queried == [str(tmp_path / "ok.py")]
