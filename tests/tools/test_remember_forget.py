"""Tests for RememberTool and ForgetTool."""

from pathlib import Path

import pytest

from cluxmate.core.retrieval_memory import RetrievalConfig, RetrievalMemory
from cluxmate.tools.forget import ForgetTool
from cluxmate.tools.remember import RememberTool


def _mem(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    cwd = tmp_path / "proj"
    cwd.mkdir()
    return RetrievalMemory(str(cwd), RetrievalConfig(tmp_path / "cfg.json"))


def test_risk_level_is_write(tmp_path, monkeypatch):
    mem = _mem(tmp_path, monkeypatch)
    assert RememberTool(retrieval=mem).risk_level == "write"
    assert ForgetTool(retrieval=mem).risk_level == "write"


@pytest.mark.asyncio
async def test_remember_tool_records(tmp_path, monkeypatch):
    mem = _mem(tmp_path, monkeypatch)
    tool = RememberTool(retrieval=mem)
    result = await tool.execute(content="Use pytest.", scope="project")
    assert "project memory" in result


@pytest.mark.asyncio
async def test_forget_tool_deletes(tmp_path, monkeypatch):
    mem = _mem(tmp_path, monkeypatch)
    result = mem.remember("temp fact")
    fact_id = result.split()[2]
    tool = ForgetTool(retrieval=mem)
    assert "Forgot" in await tool.execute(fact_id=fact_id)
