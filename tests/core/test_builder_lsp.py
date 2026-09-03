"""Tests for builder wiring of LspTool + shared LSPManager."""

import pytest

from cluxmate.core.builder import AgentBuilder, SUBAGENT_PROFILES
from cluxmate.tools.lsp_tool import LspTool


class _Provider:
    pass


def _builder(tmp_path):
    b = AgentBuilder(str(tmp_path), _Provider())
    b.with_default_tools().with_mode("default")
    return b


def test_default_tools_include_lsp(tmp_path):
    b = _builder(tmp_path)
    tools = b._get_tools()
    names = {t.name for t in tools}
    assert "lsp" in names
    lsp = next(t for t in tools if t.name == "lsp")
    assert isinstance(lsp, LspTool)
    assert lsp.risk_level == "safe"


def test_plan_tools_include_lsp(tmp_path):
    b = AgentBuilder(str(tmp_path), _Provider())
    b.with_default_tools().with_mode("plan")
    tools = b._get_tools()
    names = {t.name for t in tools}
    assert "lsp" in names
    assert "bash" not in names
    assert "write_file" not in names
    assert "task" not in names


def test_lsp_manager_is_cached_across_calls(tmp_path):
    b = _builder(tmp_path)
    mgr1 = b._lsp_manager()
    mgr2 = b._lsp_manager()
    assert mgr1 is mgr2


def test_default_mode_allows_auto_install(tmp_path):
    b = _builder(tmp_path)
    assert b._lsp_manager().auto_install is True


def test_plan_mode_disables_auto_install(tmp_path):
    # Auto-install runs installer commands — a write-class side effect — so
    # plan mode's hard isolation keeps the gate off even if lsp.json opts in.
    b = AgentBuilder(str(tmp_path), _Provider())
    b.with_default_tools().with_mode("plan")
    b._get_tools()
    assert b._lsp_manager().auto_install is False


def test_subagent_profiles_include_lsp():
    assert "lsp" in SUBAGENT_PROFILES["explore"]["tools"]
    assert "lsp" in SUBAGENT_PROFILES["general-purpose"]["tools"]


def test_child_builder_inherits_lsp_manager(tmp_path):
    b = _builder(tmp_path)
    b._get_tools()
    child = b._child_builder("explore", "child-1")
    assert child._lsp is b._lsp


def test_lsp_shutdown_is_idempotent(tmp_path):
    b = _builder(tmp_path)
    b._get_tools()
    b.lsp_shutdown()
    assert b._lsp is None
    b.lsp_shutdown()
