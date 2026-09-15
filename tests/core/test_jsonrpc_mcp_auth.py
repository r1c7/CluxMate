"""JSON-RPC surface for MCP OAuth: async start, hot swap, no secret leakage."""

import json
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import pytest

from cluxmate.core.jsonrpc_server import JsonRpcServer
from tests.core.fake_mcp_http_server import FakeOAuthServer


class _Provider:
    def set_reasoning_effort(self, effort):
        pass


def _server(tmp_path, monkeypatch, mcp_url: str) -> JsonRpcServer:
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    cwd = tmp_path / "proj"
    (cwd / ".cluxmate").mkdir(parents=True)
    (cwd / ".cluxmate" / "mcp.json").write_text(json.dumps({
        "mcpServers": {"remote": {"url": mcp_url}},
    }), encoding="utf-8")

    import cluxmate.core.jsonrpc_server as mod
    monkeypatch.setattr(mod, "_write_dict", lambda payload: sent.append(payload))
    s = JsonRpcServer()
    monkeypatch.setattr(JsonRpcServer, "_build_provider",
                        lambda self, mid: (_Provider(), "test", False, {"id": "test"}))
    monkeypatch.setattr(JsonRpcServer, "_bind_persister", lambda self: None)
    monkeypatch.setattr(JsonRpcServer, "_load_or_create_log", lambda self, sid, entry: (
        __import__("cluxmate.core.session_log", fromlist=["SessionLog"])
        .SessionLog.create(__import__("cluxmate.core.session_log", fromlist=["SessionHeader"])
                           .SessionHeader(id=sid, createdAt=0)), True))
    s._handle_initialize(1, {"session_id": "s1", "cwd": str(cwd)})
    # The deferred MCP load runs on a background thread; wait for it.
    assert s._mcp_ready.wait(timeout=10)
    return s


sent: list[dict] = []


@pytest.fixture
def fake():
    servers: list[FakeOAuthServer] = []

    def _make(**knobs) -> FakeOAuthServer:
        s = FakeOAuthServer(**knobs)
        s.start()
        servers.append(s)
        return s

    yield _make
    for s in servers:
        s.stop()


def _browser(server: FakeOAuthServer):
    def _open(url: str, *a, **kw) -> bool:
        def _go():
            try:
                urllib.request.urlopen(url, timeout=5)
            except Exception:
                pass
        threading.Thread(target=_go, daemon=True).start()
        return True
    return _open


def _wait_for(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_auth_status_lists_oauth_without_secrets(tmp_path, monkeypatch, fake):
    sent.clear()
    server = fake(require_bearer=False)
    s = _server(tmp_path, monkeypatch, server.mcp_url)
    try:
        s._dispatch(2, "mcp/auth/status", {})
        payload = [p for p in sent if p.get("id") == 2][0]
        entry = payload["result"]["servers"][0]
        assert entry["name"] == "remote"
        assert entry["oauth"]["enabled"] is True
        assert "access_token" not in json.dumps(payload)
    finally:
        s._shutdown_mcp()


def test_start_returns_immediately_and_completes_in_background(tmp_path, monkeypatch, fake):
    sent.clear()
    server = fake()
    s = _server(tmp_path, monkeypatch, server.mcp_url)
    try:
        monkeypatch.setattr(webbrowser, "open", _browser(server))
        t0 = time.time()
        s._dispatch(2, "mcp/auth/start", {"server": "remote"})
        elapsed = time.time() - t0
        assert elapsed < 1.0                      # dispatch is NOT blocked
        started = [p for p in sent if p.get("id") == 2][0]
        assert started["result"]["status"] == "started"
        assert _wait_for(lambda: any(
            p.get("method") == "mcp/auth/completed" for p in sent), timeout=15)
        done = [p for p in sent if p.get("method") == "mcp/auth/completed"][0]
        assert done["params"]["status"] == "ok"
        # The hot swap happened: the client now holds the token and its tools are
        # exposed without a re-initialize.
        assert _wait_for(lambda: s._builder.mcp_status()[0]["status"] == "connected", timeout=10)
    finally:
        s._shutdown_mcp()


def test_start_rejects_unknown_and_stdio_servers(tmp_path, monkeypatch, fake):
    sent.clear()
    server = fake(require_bearer=False)
    s = _server(tmp_path, monkeypatch, server.mcp_url)
    try:
        assert s._start_mcp_auth("nope")["status"] == "unknown"
        # A stdio server can never use OAuth — the config error wins over the
        # fact that the flow would otherwise be startable.
        s._builder.mcp_config("remote").transport = "stdio"
        assert s._start_mcp_auth("remote")["status"] == "unsupported"
        assert s._auth_inflight == set()
    finally:
        s._shutdown_mcp()


def test_cancel_interrupts_a_waiting_flow(tmp_path, monkeypatch, fake):
    sent.clear()
    server = fake()
    s = _server(tmp_path, monkeypatch, server.mcp_url)
    try:
        monkeypatch.setattr(webbrowser, "open", lambda *a, **k: True)  # nobody approves
        assert s._start_mcp_auth("remote")["status"] == "started"
        assert _wait_for(lambda: "remote" in s._auth_flows, timeout=10)
        assert s._cancel_mcp_auth("remote")["status"] == "cancelled"
        assert _wait_for(lambda: any(
            p.get("method") == "mcp/auth/completed" for p in sent), timeout=10)
        done = [p for p in sent if p.get("method") == "mcp/auth/completed"][0]
        # `cancelled`, not `failed`: Task 3's as-built delta #4 gave cancel its own
        # OAuthError kind, and Task 8's mapping keeps it (the design doc's status
        # set is {ok, denied, failed, cancelled, timeout}; the brief's `failed`
        # predates that kind). A cancel is a user action, not an error.
        assert done["params"]["status"] == "cancelled"
        assert s._cancel_mcp_auth("remote")["status"] == "not_running"
    finally:
        s._shutdown_mcp()


def test_start_twice_for_one_server_is_refused(tmp_path, monkeypatch, fake):
    """A second click while the first flow is alive must not open a second
    loopback listener / token exchange for the same server."""
    sent.clear()
    server = fake()
    s = _server(tmp_path, monkeypatch, server.mcp_url)
    try:
        monkeypatch.setattr(webbrowser, "open", lambda *a, **k: True)  # nobody approves
        assert s._start_mcp_auth("remote")["status"] == "started"
        assert s._start_mcp_auth("remote")["status"] == "already_running"
        assert s._auth_inflight == {"remote"}
        # Wait until the flow is actually waiting on its callback before
        # cancelling: a cancel that lands before authorize() resets its flag
        # would not reach this run (see test_mcp_oauth's cancel tests).
        assert _wait_for(lambda: getattr(s._auth_flows.get("remote"), "_callback", None) is not None,
                         timeout=10)
        assert s._cancel_mcp_auth("remote")["status"] == "cancelled"
        assert _wait_for(lambda: any(
            p.get("method") == "mcp/auth/completed" for p in sent), timeout=10)
        assert s._auth_inflight == set()
    finally:
        s._shutdown_mcp()


def test_authorization_url_is_announced(tmp_path, monkeypatch, fake):
    """mcp/auth/url carries the URL for a headless/hostile-browser setup — and
    the completion notification still follows exactly once."""
    sent.clear()
    server = fake()
    s = _server(tmp_path, monkeypatch, server.mcp_url)
    try:
        monkeypatch.setattr(webbrowser, "open", _browser(server))
        assert s._start_mcp_auth("remote")["status"] == "started"
        assert _wait_for(lambda: any(
            p.get("method") == "mcp/auth/completed" for p in sent), timeout=15)
        urls = [p for p in sent if p.get("method") == "mcp/auth/url"]
        assert len(urls) == 1
        assert urls[0]["params"]["server"] == "remote"
        assert urls[0]["params"]["url"].startswith(server.base_url + "/authorize?")
        completed = [p for p in sent if p.get("method") == "mcp/auth/completed"]
        assert [p["params"]["status"] for p in completed] == ["ok"]
    finally:
        s._shutdown_mcp()


def test_denied_login_keeps_the_denied_status(tmp_path, monkeypatch, fake):
    """The user refusing in the browser is reported as `denied`, not `failed` —
    the UI shows a different message for it."""
    sent.clear()
    server = fake(authorize_error="access_denied")
    s = _server(tmp_path, monkeypatch, server.mcp_url)
    try:
        monkeypatch.setattr(webbrowser, "open", _browser(server))
        assert s._start_mcp_auth("remote")["status"] == "started"
        assert _wait_for(lambda: any(
            p.get("method") == "mcp/auth/completed" for p in sent), timeout=15)
        done = [p for p in sent if p.get("method") == "mcp/auth/completed"][0]
        assert done["params"]["status"] == "denied"
        assert done["params"]["error"]
        assert s._auth_inflight == set()
    finally:
        s._shutdown_mcp()


def test_logout_clears_credentials(tmp_path, monkeypatch, fake):
    sent.clear()
    server = fake()
    s = _server(tmp_path, monkeypatch, server.mcp_url)
    try:
        monkeypatch.setattr(webbrowser, "open", _browser(server))
        s._start_mcp_auth("remote")
        assert _wait_for(lambda: any(
            p.get("method") == "mcp/auth/completed" for p in sent), timeout=15)
        from cluxmate.core.mcp_auth_store import MCPAuthStore
        assert MCPAuthStore().get("remote", server.mcp_url) is not None
        s._dispatch(3, "mcp/auth/logout", {"server": "remote"})
        assert MCPAuthStore().get("remote", server.mcp_url) is None
    finally:
        s._shutdown_mcp()
