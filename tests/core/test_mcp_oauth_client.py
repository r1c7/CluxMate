"""End-to-end tests for the MCP HTTP client's OAuth behavior.

The fake server plays both the resource server and the authorization server, so
the whole flow (401 → needs_auth → authorize → call) runs on loopback.
"""

import threading
import urllib.request
import webbrowser

import pytest

from cluxmate.core.mcp import MCPClient, MCPConfig
from cluxmate.core.mcp_auth_store import MCPAuthStore, OAuthRecord
from cluxmate.core.mcp_oauth import MCPOAuthFlow, OAuthFlowConfig
from tests.core.fake_mcp_http_server import FakeOAuthServer


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


def _client(server: FakeOAuthServer, store: MCPAuthStore, **over) -> MCPClient:
    cfg = MCPConfig(
        name="fake",
        transport="http",
        url=server.mcp_url,
        oauth=OAuthFlowConfig(server_name="fake", server_url=server.mcp_url),
        call_timeout_s=2.0,
        **over,
    )
    return MCPClient(cfg, auth_store=store)


def _authorize(server: FakeOAuthServer, store: MCPAuthStore) -> OAuthRecord:
    flow = MCPOAuthFlow(
        OAuthFlowConfig(server_name="fake", server_url=server.mcp_url),
        http_timeout=2.0, callback_timeout=2.0,
    )
    rec = flow.authorize(flow.probe())
    store.put("fake", rec)
    return rec


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


def test_401_without_credentials_is_needs_auth(tmp_path, fake):
    server = fake()
    client = _client(server, MCPAuthStore(tmp_path / "auth.json"))
    assert client.start() is False
    status = client.status()
    assert status["status"] == "needs_auth"
    assert "cluxmate mcp auth fake" in (status["error"] or "")


def test_stored_token_is_attached_before_start(tmp_path, fake, monkeypatch):
    server = fake()
    store = MCPAuthStore(tmp_path / "auth.json")
    monkeypatch.setattr(webbrowser, "open", _browser(server))
    _authorize(server, store)
    client = _client(server, store)
    assert client.start() is True
    assert client.list_tools()[0]["name"] == "echo"
    assert client.call_tool("echo", {}) == "pong"


def test_status_reports_the_oauth_block_without_secrets(tmp_path, fake, monkeypatch):
    server = fake()
    store = MCPAuthStore(tmp_path / "auth.json")
    monkeypatch.setattr(webbrowser, "open", _browser(server))
    rec = _authorize(server, store)
    client = _client(server, store)
    client.start()
    oauth = client.status()["oauth"]
    assert oauth["enabled"] is True
    assert oauth["authenticated"] is True
    assert oauth["has_refresh"] is True
    assert oauth["expires_at"] == rec.expires_at
    assert "at-1" not in str(client.status())


def test_expired_token_refreshes_silently(tmp_path, fake, monkeypatch):
    server = fake()
    store = MCPAuthStore(tmp_path / "auth.json")
    monkeypatch.setattr(webbrowser, "open", _browser(server))
    rec = _authorize(server, store)
    store.put("fake", OAuthRecord(**{**rec.__dict__, "expires_at": 1.0}))
    client = _client(server, store)
    assert client.start() is True
    refreshed = store.get("fake", server.mcp_url)
    assert refreshed.access_token == "at-1"
    assert refreshed.expires_at > 1.0


def test_expired_without_refresh_token_clears_and_needs_auth(tmp_path, fake, monkeypatch):
    server = fake()
    store = MCPAuthStore(tmp_path / "auth.json")
    monkeypatch.setattr(webbrowser, "open", _browser(server))
    rec = _authorize(server, store)
    store.put("fake", OAuthRecord(**{**rec.__dict__, "refresh_token": None, "expires_at": 1.0}))
    client = _client(server, store)
    assert client.start() is False
    assert client.status()["status"] == "needs_auth"
    assert store.get("fake", server.mcp_url) is None


def test_revoked_refresh_token_clears_credentials(tmp_path, fake, monkeypatch):
    server = fake()
    store = MCPAuthStore(tmp_path / "auth.json")
    monkeypatch.setattr(webbrowser, "open", _browser(server))
    rec = _authorize(server, store)
    store.put("fake", OAuthRecord(**{**rec.__dict__, "refresh_token": "stale", "expires_at": 1.0}))
    client = _client(server, store)
    assert client.start() is False
    assert client.status()["status"] == "needs_auth"
    assert store.get("fake", server.mcp_url) is None


def test_transient_refresh_failure_keeps_credentials(tmp_path, fake, monkeypatch):
    server = fake(token_status=500)
    store = MCPAuthStore(tmp_path / "auth.json")
    store.put("fake", OAuthRecord(
        server_url=server.mcp_url, access_token="at-old",
        token_endpoint=f"{server.base_url}/token", client_id="cid",
        refresh_token="rt-1", expires_at=1.0,
    ))
    client = _client(server, store)
    assert client.start() is False
    status = client.status()
    assert status["status"] == "failed"          # NOT needs_auth
    assert "HTTP 500" in (status["error"] or "")
    assert store.get("fake", server.mcp_url) is not None


def test_mid_session_401_forces_one_refresh_and_retries(tmp_path, fake, monkeypatch):
    server = fake()
    store = MCPAuthStore(tmp_path / "auth.json")
    monkeypatch.setattr(webbrowser, "open", _browser(server))
    rec = _authorize(server, store)
    client = _client(server, store)
    assert client.start() is True
    # The server starts rejecting the old token; only a fresh one works.
    server._srv.knobs["access_token"] = "at-2"
    store.put("fake", OAuthRecord(**{**rec.__dict__, "expires_at": rec.expires_at}))
    assert client.call_tool("echo", {}) == "pong"
    assert store.get("fake", server.mcp_url).access_token == "at-2"


def test_second_401_gives_an_actionable_error(tmp_path, fake, monkeypatch):
    server = fake()
    store = MCPAuthStore(tmp_path / "auth.json")
    monkeypatch.setattr(webbrowser, "open", _browser(server))
    rec = _authorize(server, store)
    store.put("fake", OAuthRecord(**{**rec.__dict__, "refresh_token": None}))
    client = _client(server, store)
    server._srv.knobs["access_token"] = "at-2"   # the stored token is now stale
    assert client.start() is False
    assert client.status()["status"] == "needs_auth"


def test_static_header_suppresses_oauth_and_is_reported(tmp_path, fake):
    server = fake(require_bearer=False)
    store = MCPAuthStore(tmp_path / "auth.json")
    cfg = MCPConfig(
        name="fake", transport="http", url=server.mcp_url,
        headers={"Authorization": "Bearer static"}, call_timeout_s=2.0,
    )
    client = MCPClient(cfg, auth_store=store)
    assert client.status()["oauth"] is None
    assert client.start() is True


def test_conflict_is_reported_when_both_are_present(tmp_path, fake):
    server = fake(require_bearer=False)
    cfg = MCPConfig(
        name="fake", transport="http", url=server.mcp_url,
        headers={"Authorization": "Bearer static"},
        oauth=OAuthFlowConfig(server_name="fake", server_url=server.mcp_url),
        call_timeout_s=2.0,
    )
    client = MCPClient(cfg, auth_store=MCPAuthStore(tmp_path / "auth.json"))
    assert client.status()["oauth"]["conflict"] == "static_header"


def test_stdio_client_untouched(tmp_path):
    cfg = MCPConfig(name="local", transport="stdio", command="nope", args=[],
                    oauth_error="oauth is only supported for remote (url) MCP servers")
    client = MCPClient(cfg, auth_store=MCPAuthStore(tmp_path / "auth.json"))
    assert client.start() is False
    assert client.status()["status"] == "failed"
    assert "remote" in client.status()["error"]
    assert client.status()["oauth"] is None
