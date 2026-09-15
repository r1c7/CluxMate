"""Tests for the MCP OAuth protocol engine (no real network, no browser)."""

import pytest

from cluxmate.core.mcp_auth_store import MCPAuthStore, OAuthRecord
from cluxmate.core.mcp_oauth import (
    Challenge,
    MCPOAuthFlow,
    OAuthError,
    OAuthFlowConfig,
    parse_www_authenticate,
    redact,
)
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


def _flow(server: FakeOAuthServer, **over) -> MCPOAuthFlow:
    # open_browser=True so the authorize() tests can stand in a fake browser by
    # patching webbrowser.open; every test that must NOT reach a browser either
    # patches it out (test_authorize_callback_timeout) or builds its own flow
    # with open_browser=False (test_authorize_without_browser_prints_url).
    cfg = OAuthFlowConfig(server_name="fake", server_url=server.mcp_url, **over)
    return MCPOAuthFlow(cfg, http_timeout=2.0, callback_timeout=2.0, open_browser=True)


def test_parse_www_authenticate():
    c = parse_www_authenticate(
        'Bearer resource_metadata="https://a.example.com/prm", scope="read write"'
    )
    assert c.resource_metadata == "https://a.example.com/prm"
    assert c.scope == "read write"
    assert parse_www_authenticate("Bearer").resource_metadata is None
    assert parse_www_authenticate("").scope is None


def test_redact_removes_secret_values():
    # redact() skips only None/"" entries, so the fixture uses secrets the guard
    # cannot confuse with prose: each is at least 4 chars and occurs exactly once,
    # and one replacement inserts exactly one "***".
    out = redact("failed with token at-1 and secret rt-2", ["at-1", "rt-2", None, ""])
    assert "at-1" not in out and "rt-2" not in out
    assert out.count("***") == 2


def test_redact_replaces_even_short_secrets():
    assert redact("bad token xy", ["xy"]) == "bad token ***"
    assert redact("nothing to hide", [None, ""]) == "nothing to hide"


def test_probe_reads_the_challenge(fake):
    server = fake()
    challenge = _flow(server).probe()
    assert challenge is not None
    assert challenge.resource_metadata == f"{server.base_url}/.well-known/oauth-protected-resource/mcp"
    assert challenge.scope == "read write"


def test_discover_follows_challenge_then_as_metadata(fake):
    server = fake()
    flow = _flow(server)
    d = flow.discover(flow.probe())
    assert d.issuer == server.base_url
    assert d.authorization_endpoint == f"{server.base_url}/authorize"
    assert d.token_endpoint == f"{server.base_url}/token"
    assert d.registration_endpoint == f"{server.base_url}/register"
    assert d.resource == f"{server.base_url}/mcp"


def test_probe_returns_none_without_a_challenge_header(fake):
    server = fake(challenge=False)
    assert _flow(server).probe() is None


def test_discover_uses_the_challenge_when_probe_finds_one(fake):
    server = fake()
    d = _flow(server).discover(_flow(server).probe())
    assert d.token_endpoint == f"{server.base_url}/token"


def test_cross_origin_resource_metadata_is_rejected(fake):
    other = fake()          # a second server, i.e. a different origin
    server = fake(resource_metadata_url=f"{other.base_url}/.well-known/oauth-protected-resource/mcp")
    with pytest.raises(OAuthError) as e:
        _flow(server).discover(Challenge(resource_metadata=(
            f"{other.base_url}/.well-known/oauth-protected-resource/mcp")))
    assert e.value.kind == "discovery"
    assert "same-origin" in str(e.value)
    # and the foreign origin was never contacted (the challenge URL points
    # there, so a broken implementation would fetch it)
    assert other.paths() == []


def test_issuer_mismatch_is_rejected(fake):
    server = fake(issuer_mismatch=True)
    with pytest.raises(OAuthError) as e:
        _flow(server).discover()
    assert e.value.kind == "discovery"
    assert "issuer" in str(e.value)


def test_register_prefers_configured_client_id(fake):
    server = fake()
    flow = _flow(server, client_id="mine", client_secret="s3cret")
    reg = flow.register_client(flow.discover())
    assert reg.client_id == "mine" and reg.client_secret == "s3cret"
    assert server.registrations == []


def test_register_uses_dcr_when_no_client_id(fake):
    server = fake()
    flow = _flow(server)
    reg = flow.register_client(flow.discover())
    assert reg.client_id == "dcr-client"
    body = server.registrations[0]
    assert body["client_name"] == "CluxMate"
    assert body["grant_types"] == ["authorization_code", "refresh_token"]
    assert body["token_endpoint_auth_method"] == "none"
    assert body["redirect_uris"][0].startswith("http://127.0.0.1:")


def test_register_auth_method_precedence(fake):
    server = fake(auth_methods=["client_secret_basic", "client_secret_post", "none"])
    _flow(server).register_client(_flow(server).discover())
    assert server.registrations[0]["token_endpoint_auth_method"] == "client_secret_basic"


def test_register_without_registration_endpoint_asks_for_client_id(fake):
    server = fake(registration=False)
    with pytest.raises(OAuthError) as e:
        _flow(server).register_client(_flow(server).discover())
    assert e.value.kind == "registration"
    assert "client_id" in str(e.value)


def test_off_origin_authorization_endpoint_is_rejected(fake):
    other = fake()
    server = fake(endpoint_origins={"authorization_endpoint": other.base_url}, registration=False)
    with pytest.raises(OAuthError) as e:
        _flow(server).discover()
    assert e.value.kind == "discovery"
    assert "authorization_endpoint" in str(e.value)


def test_off_origin_token_endpoint_is_rejected(fake):
    other = fake()
    server = fake(endpoint_origins={"token_endpoint": other.base_url}, registration=False)
    with pytest.raises(OAuthError) as e:
        _flow(server).discover()
    assert "token_endpoint" in str(e.value)


def test_off_origin_registration_endpoint_is_rejected(fake):
    other = fake()
    server = fake(endpoint_origins={"registration_endpoint": other.base_url})
    with pytest.raises(OAuthError) as e:
        _flow(server).discover()
    assert "registration_endpoint" in str(e.value)


def test_off_origin_discovery_redirect_is_rejected(fake):
    other = fake()
    server = fake(redirect_prm_to=f"{other.base_url}/.well-known/oauth-protected-resource/mcp")
    with pytest.raises(OAuthError) as e:
        _flow(server).discover()
    assert e.value.kind == "discovery"
    assert "off-origin" in str(e.value)
    assert other.paths() == []          # the foreign origin was never contacted


def test_openid_configuration_is_used_when_the_oas_path_is_absent(fake):
    server = fake(serve_oas=False)
    d = _flow(server).discover()
    assert d.token_endpoint == f"{server.base_url}/token"
    assert "/.well-known/openid-configuration" in server.paths()


# ── PKCE + loopback callback + token exchange ──────────────────────

import threading
import urllib.request
import webbrowser

from cluxmate.core.mcp_oauth import refresh_access_token


def _drive_browser(server: FakeOAuthServer):
    """Return a webbrowser.open replacement that 'is' the user's browser: it
    fetches the authorization URL, which 302s to the loopback callback."""
    def _open(url: str, *a, **kw) -> bool:
        def _go():
            try:
                urllib.request.urlopen(url, timeout=5)
            except Exception:
                pass
        threading.Thread(target=_go, daemon=True).start()
        return True
    return _open


def _authorize(server: FakeOAuthServer, monkeypatch, **over) -> OAuthRecord:
    flow = _flow(server, **over)
    monkeypatch.setattr(webbrowser, "open", _drive_browser(server))
    return flow.authorize()


def test_authorize_happy_path(fake, monkeypatch, tmp_path):
    server = fake()
    rec = _authorize(server, monkeypatch)
    assert rec.access_token == "at-1"
    assert rec.refresh_token == "rt-1"
    assert rec.server_url == server.mcp_url
    assert rec.token_endpoint == f"{server.base_url}/token"
    assert rec.issuer == server.base_url
    assert rec.expires_at is not None
    # the authorization request carried PKCE S256 + state + resource
    q = server.authorize_query
    assert q["code_challenge_method"] == ["S256"]
    assert q["response_type"] == ["code"]
    assert q["resource"] == [server.mcp_url]
    # and the exchange proved the verifier
    form = [f for f in server.token_forms if f["grant_type"] == "authorization_code"][0]
    assert form["redirect_uri"].endswith("/oauth/callback")
    assert form["resource"] == server.mcp_url


def test_authorize_uses_challenge_scope(fake, monkeypatch):
    server = fake(token_scope="read")
    flow = _flow(server, scopes="write")
    monkeypatch.setattr(webbrowser, "open", _drive_browser(server))
    flow.authorize(challenge=Challenge(resource_metadata=None, scope="read write"))
    assert server.authorize_query["scope"] == ["read write"]


def test_authorize_falls_back_to_configured_scope(fake, monkeypatch):
    server = fake()
    flow = _flow(server, scopes="write")
    monkeypatch.setattr(webbrowser, "open", _drive_browser(server))
    flow.authorize()
    assert server.authorize_query["scope"] == ["write"]


def test_authorize_state_mismatch_aborts_without_token_request(fake, monkeypatch):
    server = fake(authorize_redirect_state=True)
    with pytest.raises(OAuthError) as e:
        _authorize(server, monkeypatch)
    assert e.value.kind in ("authorize", "denied")
    assert server.token_forms == []


def test_authorize_user_denies(fake, monkeypatch):
    server = fake(authorize_error="access_denied")
    with pytest.raises(OAuthError) as e:
        _authorize(server, monkeypatch)
    assert e.value.kind == "denied"
    assert "access_denied" in str(e.value)


def test_authorize_callback_timeout(fake, monkeypatch):
    server = fake()
    flow = _flow(server)
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: True)  # nobody comes back
    with pytest.raises(OAuthError) as e:
        flow.authorize()
    assert e.value.kind == "timeout"


def test_authorize_without_browser_prints_url(fake):
    server = fake()
    seen: list[str] = []
    flow = MCPOAuthFlow(
        OAuthFlowConfig(server_name="fake", server_url=server.mcp_url),
        http_timeout=2.0, callback_timeout=0.3, open_browser=False,
        on_authorize_url=seen.append,
    )
    with pytest.raises(OAuthError):
        flow.authorize()
    assert seen and seen[0].startswith(f"{server.base_url}/authorize?")


def test_authorize_rejects_plain_pkce_only_server(fake, monkeypatch):
    server = fake(pkce_methods=["plain"])
    flow = _flow(server)
    monkeypatch.setattr(webbrowser, "open", _drive_browser(server))
    with pytest.raises(OAuthError) as e:
        flow.authorize()
    assert "S256" in str(e.value)
    assert server.token_forms == []          # nothing was exchanged


def test_refresh_rotates_and_reports_expiry(fake):
    server = fake()
    rec = OAuthRecord(
        server_url=server.mcp_url, access_token="old", token_endpoint=f"{server.base_url}/token",
        client_id="cid", refresh_token="rt-1", expires_at=1.0,
    )
    out = refresh_access_token(rec, http_timeout=2.0)
    assert out.access_token == "at-1"
    assert out.refresh_token == "rt-1"
    assert out.expires_at > 1.0


def test_refresh_invalid_grant_is_rejected(fake):
    server = fake(refresh_token="rt-1")
    rec = OAuthRecord(
        server_url=server.mcp_url, access_token="old", token_endpoint=f"{server.base_url}/token",
        client_id="cid", refresh_token="stale", expires_at=1.0,
    )
    with pytest.raises(OAuthError) as e:
        refresh_access_token(rec, http_timeout=2.0)
    assert e.value.kind == "refresh_rejected"


def test_refresh_5xx_is_transient(fake):
    server = fake(token_status=500, token_error_body={"error": "server_error"})
    rec = OAuthRecord(
        server_url=server.mcp_url, access_token="old", token_endpoint=f"{server.base_url}/token",
        client_id="cid", refresh_token="rt-1", expires_at=1.0,
    )
    with pytest.raises(OAuthError) as e:
        refresh_access_token(rec, http_timeout=2.0)
    assert e.value.kind == "refresh_transient"


def test_errors_never_contain_the_token(fake):
    server = fake(token_status=400, token_error_body={"error": "invalid_grant"})
    rec = OAuthRecord(
        server_url=server.mcp_url, access_token="SUPER-SECRET-ACCESS",
        token_endpoint=f"{server.base_url}/token", client_id="cid",
        refresh_token="SUPER-SECRET-REFRESH", expires_at=1.0,
    )
    with pytest.raises(OAuthError) as e:
        refresh_access_token(rec, http_timeout=2.0)
    assert "SUPER-SECRET" not in str(e.value)


def test_refresh_error_body_echoing_the_token_is_redacted(fake):
    """The test above cannot fail: this fake AS never echoes the secret, so its
    400 body has nothing to leak. Here the AS echoes the refresh token back in
    error_description — the case redaction actually defends against."""
    server = fake(token_status=400, token_error_body={
        "error": "invalid_grant",
        "error_description": "refresh token SUPER-SECRET-REFRESH was revoked",
    })
    rec = OAuthRecord(
        server_url=server.mcp_url, access_token="old",
        token_endpoint=f"{server.base_url}/token", client_id="cid",
        refresh_token="SUPER-SECRET-REFRESH", expires_at=1.0,
    )
    with pytest.raises(OAuthError) as e:
        refresh_access_token(rec, http_timeout=2.0)
    assert e.value.kind == "refresh_rejected"
    assert "SUPER-SECRET-REFRESH" not in str(e.value)
    assert "revoked" in str(e.value)        # the rest of the AS body survives
