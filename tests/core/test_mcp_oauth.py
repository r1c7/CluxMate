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
    cfg = OAuthFlowConfig(server_name="fake", server_url=server.mcp_url, **over)
    return MCPOAuthFlow(cfg, http_timeout=2.0, callback_timeout=2.0, open_browser=False)


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


def test_discover_without_challenge_uses_well_known_candidates(fake):
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
