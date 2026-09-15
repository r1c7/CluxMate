"""Reloading a single MCP server after its credentials changed."""

import json
from pathlib import Path

import pytest

from cluxmate.core.builder import AgentBuilder
from cluxmate.core.mcp import MCPConfig, MCPManager


def _manager_with_one_remote(monkeypatch, tmp_path, knobs: dict | None = None):
    from tests.core.fake_mcp_http_server import FakeOAuthServer
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    server = FakeOAuthServer(require_bearer=False, **(knobs or {}))
    server.start()
    project = tmp_path / "proj"
    (project / ".cluxmate").mkdir(parents=True)
    (project / ".cluxmate" / "mcp.json").write_text(json.dumps({
        "mcpServers": {"remote": {"url": server.mcp_url}},
    }), encoding="utf-8")
    mgr = MCPManager(str(project))
    mgr.load()
    return server, mgr


def test_config_accessor(tmp_path, monkeypatch):
    server, mgr = _manager_with_one_remote(monkeypatch, tmp_path)
    try:
        cfg = mgr.config("remote")
        assert isinstance(cfg, MCPConfig)
        assert cfg.url == server.mcp_url
        assert mgr.config("nope") is None
    finally:
        mgr.shutdown()
        server.stop()


def test_reload_client_rebuilds_the_tool_list(tmp_path, monkeypatch):
    server, mgr = _manager_with_one_remote(monkeypatch, tmp_path)
    try:
        assert [t.name for t in mgr.list_tools()] == ["mcp__remote__echo"]
        changed = mgr.reload_client("remote")
        assert changed is False            # same server, same tools
        assert [t.name for t in mgr.list_tools()] == ["mcp__remote__echo"]
    finally:
        mgr.shutdown()
        server.stop()


def test_reload_client_unknown_server_is_a_noop(tmp_path, monkeypatch):
    server, mgr = _manager_with_one_remote(monkeypatch, tmp_path)
    try:
        assert mgr.reload_client("nope") is False
    finally:
        mgr.shutdown()
        server.stop()


def test_reload_client_drops_tools_when_the_server_now_fails(tmp_path, monkeypatch):
    server, mgr = _manager_with_one_remote(monkeypatch, tmp_path)
    try:
        assert mgr.list_tools()
        server._srv.knobs["require_bearer"] = True   # now 401s, no credentials
        assert mgr.reload_client("remote") is True   # tool set changed (emptied)
        assert mgr.list_tools() == []
        assert mgr.status()[0]["status"] == "needs_auth"
    finally:
        mgr.shutdown()
        server.stop()


def test_builder_reload_without_a_manager(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    # AgentBuilder requires a provider (the brief's snippet omitted it — see the
    # task-7 report, "Deviations"). A bare object() is enough: none of the MCP
    # accessors touched below ever reads it (tests/core/test_skills.py:147 does
    # the same).
    builder = AgentBuilder(cwd=str(tmp_path), provider=object())
    assert builder.mcp_config("remote") is None
    assert builder.mcp_challenge("remote") is None
    assert builder.reload_mcp_server("remote") is False
