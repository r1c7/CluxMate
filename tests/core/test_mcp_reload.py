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


def test_reload_after_shutdown_does_not_respawn(tmp_path, monkeypatch):
    server, mgr = _manager_with_one_remote(monkeypatch, tmp_path)
    try:
        mgr.shutdown()
        assert mgr.reload_client("remote") is False
        assert mgr.list_tools() == []
        assert mgr.status() == []
    finally:
        server.stop()


def test_reload_racing_shutdown_does_not_leak_a_client(tmp_path, monkeypatch):
    import cluxmate.core.mcp as mcp_mod

    server, mgr = _manager_with_one_remote(monkeypatch, tmp_path)
    try:
        spawned: list = []
        real_cls = mcp_mod.MCPClient

        class _Spy(real_cls):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                spawned.append(self)

        monkeypatch.setattr(mcp_mod, "MCPClient", _Spy)
        original = mcp_mod.MCPManager._start_and_handshake

        def _handshake_then_shutdown(self, client):
            original(self, client)
            self.shutdown()          # the concurrent teardown, mid-reload

        monkeypatch.setattr(mcp_mod.MCPManager, "_start_and_handshake",
                            _handshake_then_shutdown)

        assert mgr.reload_client("remote") is False
        assert mgr._clients == {}
        assert spawned, "the spy must have seen the reload's client"
        assert all(c._http is None and c._proc is None for c in spawned), [
            (c._http, c._proc) for c in spawned
        ]
    finally:
        server.stop()


def test_builder_reload_refuses_after_builder_shutdown(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    builder = AgentBuilder(cwd=str(tmp_path), provider=object())
    called: list[str] = []

    class _Mgr:
        def reload_client(self, name):
            called.append(name)
            return True

    builder._mcp = _Mgr()
    builder._mcp_closed = True
    assert builder.reload_mcp_server("remote") is False
    assert called == []
