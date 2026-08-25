"""Tests for UpdateMemoryTool — appending durable entries to AGENTS.md."""

from pathlib import Path

import pytest

from cluxmate.core.memory import MEMORY_FILENAME, LEGACY_FILENAME
from cluxmate.tools.update_memory import UpdateMemoryTool


def _redirect_home(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """Redirect ~ under tmp_path. Returns (global_dir, project_cwd)."""
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    cwd = tmp_path / "proj"
    cwd.mkdir()
    return home / ".cluxmate", cwd


def test_risk_level_is_write():
    assert UpdateMemoryTool(cwd=".").risk_level == "write"


@pytest.mark.asyncio
async def test_execute_writes_project(tmp_path, monkeypatch):
    _, cwd = _redirect_home(tmp_path, monkeypatch)
    tool = UpdateMemoryTool(cwd=str(cwd))
    result = await tool.execute(content="Use pytest, not unittest.")
    target = cwd / MEMORY_FILENAME
    assert str(target) in result
    assert "project memory" in result
    assert "Use pytest, not unittest." in target.read_text("utf-8")


@pytest.mark.asyncio
async def test_execute_writes_global(tmp_path, monkeypatch):
    gdir, cwd = _redirect_home(tmp_path, monkeypatch)
    tool = UpdateMemoryTool(cwd=str(cwd))
    result = await tool.execute(content="Prefer terse replies.", scope="global")
    target = gdir / MEMORY_FILENAME
    assert str(target) in result
    assert "global memory" in result
    assert "Prefer terse replies." in target.read_text("utf-8")


@pytest.mark.asyncio
async def test_execute_empty_content_is_error(tmp_path, monkeypatch):
    _, cwd = _redirect_home(tmp_path, monkeypatch)
    tool = UpdateMemoryTool(cwd=str(cwd))
    result = await tool.execute(content="   ")
    assert "Error" in result
    assert not (cwd / MEMORY_FILENAME).exists()


@pytest.mark.asyncio
async def test_execute_bad_scope_falls_back_to_project(tmp_path, monkeypatch):
    _, cwd = _redirect_home(tmp_path, monkeypatch)
    tool = UpdateMemoryTool(cwd=str(cwd))
    result = await tool.execute(content="Something.", scope="bogus")
    assert (cwd / MEMORY_FILENAME).is_file()
    assert "project memory" in result


@pytest.mark.asyncio
async def test_execute_writes_agent_not_claude(tmp_path, monkeypatch):
    _, cwd = _redirect_home(tmp_path, monkeypatch)
    # A legacy CLAUDE.md already exists; update_memory must target AGENTS.md.
    (cwd / LEGACY_FILENAME).write_text("Legacy claude rule.", encoding="utf-8")
    tool = UpdateMemoryTool(cwd=str(cwd))
    result = await tool.execute(content="New agent rule.")
    assert (cwd / MEMORY_FILENAME).is_file()
    assert "New agent rule." in (cwd / MEMORY_FILENAME).read_text("utf-8")
    assert (cwd / LEGACY_FILENAME).read_text("utf-8") == "Legacy claude rule."
