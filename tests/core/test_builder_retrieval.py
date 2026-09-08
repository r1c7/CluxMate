"""Tests for builder wiring of retrieval memory."""

import json
from pathlib import Path

from cluxmate.core.builder import AgentBuilder
from cluxmate.core.retrieval_memory import RetrievalConfig, RetrievalMemory


class _Provider:
    pass


def _builder(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    cwd = tmp_path / "proj"
    cwd.mkdir()
    return AgentBuilder(str(cwd), _Provider())


def _enabled_config(tmp_path) -> RetrievalConfig:
    p = tmp_path / "retrieval-memory.json"
    p.write_text(json.dumps({"enabled": True}), encoding="utf-8")
    return RetrievalConfig(p)


def test_no_config_registers_no_tools(tmp_path, monkeypatch):
    b = _builder(tmp_path, monkeypatch).with_default_tools().with_mode("default")
    names = {t.name for t in b._get_tools()}
    assert "remember" not in names
    assert "forget" not in names
    assert b._retrieval_manager() is None


def test_enabled_registers_tools_and_build_has_retrieval(tmp_path, monkeypatch):
    b = _builder(tmp_path, monkeypatch).with_default_tools().with_mode("default")
    b.with_retrieval_memory(_enabled_config(tmp_path))
    names = {t.name for t in b._get_tools()}
    assert "remember" in names
    assert "forget" in names
    assert isinstance(b._retrieval_manager(), RetrievalMemory)


def test_plan_mode_excludes_tools(tmp_path, monkeypatch):
    b = _builder(tmp_path, monkeypatch).with_default_tools().with_mode("plan")
    b.with_retrieval_memory(_enabled_config(tmp_path))
    names = {t.name for t in b._get_tools()}
    assert "remember" not in names
    assert "forget" not in names


def test_child_builder_does_not_inherit_retrieval(tmp_path, monkeypatch):
    b = _builder(tmp_path, monkeypatch).with_default_tools().with_mode("default")
    b.with_retrieval_memory(_enabled_config(tmp_path))
    child = b._child_builder("explore", "child-1")
    assert child._retrieval_config is None
    assert child._retrieval_manager() is None


def test_system_prompt_mentions_retrieval_when_enabled(tmp_path, monkeypatch):
    b = _builder(tmp_path, monkeypatch).with_default_tools().with_mode("default")
    b.with_retrieval_memory(_enabled_config(tmp_path))
    prompt = b._render_system_prompt(b._get_tools())
    assert "<retrieval_memory>" in prompt
    # The decision rule: both memory systems named, plus the low-authority guard.
    assert "`update_memory`" in prompt
    assert "`remember`" in prompt
    assert "`forget`" in prompt
    assert "AGENTS.md" in prompt
    assert "never override" in prompt


def test_system_prompt_omits_retrieval_when_disabled(tmp_path, monkeypatch):
    # No retrieval config attached → remember/forget are not registered → the
    # how-to block must be absent (zero behavior change when the feature is off).
    b = _builder(tmp_path, monkeypatch).with_default_tools().with_mode("default")
    prompt = b._render_system_prompt(b._get_tools())
    assert "<retrieval_memory>" not in prompt


def test_child_builder_prompt_omits_retrieval(tmp_path, monkeypatch):
    # Children never register remember/forget, so a child-rendered system prompt
    # must not advertise the retrieval-memory decision rule (mirrors the
    # test_builder_todo child-prompt assertion).
    b = _builder(tmp_path, monkeypatch).with_default_tools().with_mode("default")
    b.with_retrieval_memory(_enabled_config(tmp_path))
    child = b._child_builder("general-purpose", "child-1")
    prompt = child._render_system_prompt(child._get_tools())
    assert "<retrieval_memory>" not in prompt
