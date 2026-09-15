"""Tests for the MCP OAuth protocol engine (no real network, no browser)."""

import json

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
import time
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
    assert rec.token_auth_method == "none"    # the method this AS advertised
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


def test_scope_comes_from_as_metadata_when_nothing_else_is_given(fake, monkeypatch):
    server = fake()
    flow = MCPOAuthFlow(
        OAuthFlowConfig(server_name="fake", server_url=server.mcp_url),
        http_timeout=2.0, callback_timeout=2.0,
    )
    monkeypatch.setattr(webbrowser, "open", _drive_browser(server))
    flow.authorize()
    assert server.authorize_query["scope"] == ["read write"]


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
    # Its own flow: the shared helper waits 2 s, which made this the slowest
    # test in the file for no extra coverage.
    flow = MCPOAuthFlow(
        OAuthFlowConfig(server_name="fake", server_url=server.mcp_url),
        http_timeout=2.0, callback_timeout=0.3,
    )
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: True)  # nobody comes back
    with pytest.raises(OAuthError) as e:
        flow.authorize()
    assert e.value.kind == "timeout"


def test_cancel_interrupts_the_wait_with_its_own_kind(fake, monkeypatch):
    server = fake()
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: True)  # nobody approves
    flow = _flow(server)
    box: list[OAuthError] = []

    def _run():
        try:
            flow.authorize()
        except OAuthError as e:
            box.append(e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    deadline = time.time() + 5
    while getattr(flow, "_callback", None) is None and time.time() < deadline:
        time.sleep(0.02)
    flow.cancel()
    t.join(timeout=5)
    assert box and box[0].kind == "cancelled"


def test_a_busy_callback_port_raises_an_oauth_error(fake, monkeypatch):
    import socket

    server = fake()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: True)
    try:
        flow = _flow(server, callback_port=port)
        with pytest.raises(OAuthError) as e:
            flow.authorize()
        assert e.value.kind == "authorize"
        assert str(port) in str(e.value)
    finally:
        sock.close()


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


def test_authorization_code_is_redacted(fake, monkeypatch):
    """The AS may echo the authorization code it just rejected; the code is a
    credential and must not survive into the user-visible error."""
    server = fake(token_status=400, token_error_body={
        "error": "invalid_grant",
        "error_description": "authorization code code-1 is invalid",
    })
    with pytest.raises(OAuthError) as e:
        _authorize(server, monkeypatch)
    assert "code-1" not in str(e.value)


def test_authorize_records_the_auth_method_it_used(fake, monkeypatch):
    """The exchange must authenticate the way the AS advertises, and the stored
    record must remember that choice so refresh can repeat it."""
    server = fake(auth_methods=["client_secret_basic"])
    rec = _authorize(server, monkeypatch)
    assert rec.token_auth_method == "client_secret_basic"
    sent = [r for r in server.requests if r["path"] == "/token"][-1]
    assert sent["headers"].get("Authorization", "").startswith("Basic ")
    assert "dcr-secret" not in sent["body"]


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


def test_refresh_uses_basic_auth_when_the_record_says_so(fake):
    server = fake()
    rec = OAuthRecord(
        server_url=server.mcp_url, access_token="old",
        token_endpoint=f"{server.base_url}/token", client_id="cid",
        client_secret="SUPER-SECRET-CLIENT", token_auth_method="client_secret_basic",
        refresh_token="rt-1", expires_at=1.0,
    )
    out = refresh_access_token(rec, http_timeout=2.0)
    assert out.access_token == "at-1"
    sent = server.requests[-1]
    assert sent["headers"].get("Authorization", "").startswith("Basic ")
    assert "SUPER-SECRET-CLIENT" not in sent["body"]
    assert out.token_auth_method == "client_secret_basic"


def test_refresh_redacts_the_basic_secret(fake):
    """With basic auth the secret is not in the form, so form-only redaction
    would let an AS that echoes the client secret leak it."""
    server = fake(token_status=400, token_error_body={
        "error": "invalid_client",
        "error_description": "client SUPER-SECRET-CLIENT is unknown",
    })
    rec = OAuthRecord(
        server_url=server.mcp_url, access_token="old",
        token_endpoint=f"{server.base_url}/token", client_id="cid",
        client_secret="SUPER-SECRET-CLIENT", token_auth_method="client_secret_basic",
        refresh_token="rt-1", expires_at=1.0,
    )
    with pytest.raises(OAuthError) as e:
        refresh_access_token(rec, http_timeout=2.0)
    assert e.value.kind == "refresh_rejected"
    assert "SUPER-SECRET-CLIENT" not in str(e.value)


def test_refresh_keeps_posting_the_secret_when_the_record_says_post(fake):
    server = fake()
    rec = OAuthRecord(
        server_url=server.mcp_url, access_token="old",
        token_endpoint=f"{server.base_url}/token", client_id="cid",
        client_secret="SUPER-SECRET-CLIENT", token_auth_method="client_secret_post",
        refresh_token="rt-1", expires_at=1.0,
    )
    refresh_access_token(rec, http_timeout=2.0)
    sent = server.requests[-1]
    assert "SUPER-SECRET-CLIENT" in sent["body"]
    assert "Authorization" not in sent["headers"]


def test_refresh_sends_the_resource(fake):
    server = fake()
    rec = OAuthRecord(
        server_url=server.mcp_url, access_token="old", resource=server.mcp_url,
        token_endpoint=f"{server.base_url}/token", client_id="cid",
        refresh_token="rt-1", expires_at=1.0,
    )
    refresh_access_token(rec, http_timeout=2.0)
    assert server.token_forms[-1]["resource"] == server.mcp_url


def test_token_auth_method_survives_the_store(tmp_path):
    """The refresh path has no discovery document, so the method chosen at
    authorization time must come back out of ~/.cluxmate/mcp-auth.json."""
    url = "http://127.0.0.1:9/mcp"
    store = MCPAuthStore(tmp_path / "mcp-auth.json")
    store.put("fake", OAuthRecord(
        server_url=url, access_token="at", token_endpoint="http://127.0.0.1:9/token",
        client_id="cid", client_secret="sec", refresh_token="rt",
        token_auth_method="client_secret_basic",
    ))
    got = store.get("fake", url)
    assert got is not None
    assert got.token_auth_method == "client_secret_basic"


def test_token_auth_method_defaults_to_none_for_older_records(tmp_path):
    """A file written before this field existed must read back as "none", not
    crash or silently claim a secret-auth method."""
    url = "http://127.0.0.1:9/mcp"
    p = tmp_path / "mcp-auth.json"
    p.write_text(json.dumps({"version": 1, "servers": {"fake": {
        "server_url": url,
        "issuer": "http://127.0.0.1:9",
        "token_endpoint": "http://127.0.0.1:9/token",
        "client": {"client_id": "cid", "client_secret": "sec"},
        "tokens": {"access_token": "at", "refresh_token": "rt", "expires_at": 1.0},
    }}}), encoding="utf-8")
    got = MCPAuthStore(p).get("fake", url)
    assert got is not None
    assert got.token_auth_method == "none"


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
    # The AS echoes the refresh token it refused in error_description: with the
    # redaction in refresh_access_token removed, "SUPER-SECRET-REFRESH" reaches
    # the user verbatim and this test fails. (An echo of a value the request
    # never carried — e.g. "at-1" — would leave it vacuous again.)
    server = fake(token_status=400, token_error_body={
        "error": "invalid_grant",
        "error_description": "token SUPER-SECRET-REFRESH was revoked",
    })
    rec = OAuthRecord(
        server_url=server.mcp_url, access_token="SUPER-SECRET-ACCESS",
        token_endpoint=f"{server.base_url}/token", client_id="cid",
        refresh_token="SUPER-SECRET-REFRESH", expires_at=1.0,
    )
    with pytest.raises(OAuthError) as e:
        refresh_access_token(rec, http_timeout=2.0)
    assert "SUPER-SECRET" not in str(e.value)
    assert "revoked" in str(e.value)        # the rest of the AS body survives


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
