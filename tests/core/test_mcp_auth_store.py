"""Tests for the MCP OAuth credential store (~/.cluxmate/mcp-auth.json)."""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from cluxmate.core.mcp_auth_store import (
    OAuthRecord,
    MCPAuthStore,
    default_path,
    normalize_url,
    same_origin,
)

_URL = "https://mcp.example.com/mcp"


def _record(**over) -> OAuthRecord:
    values = dict(
        server_url=_URL,
        access_token="at-1",
        token_endpoint="https://as.example.com/token",
        issuer="https://as.example.com",
        resource=_URL,
        client_id="cid",
        client_secret="sec",
        refresh_token="rt-1",
        expires_at=1000.0,
        scope="read write",
    )
    values.update(over)
    return OAuthRecord(**values)


def test_round_trip(tmp_path):
    store = MCPAuthStore(tmp_path / "mcp-auth.json")
    store.put("linear", _record())
    got = store.get("linear", _URL)
    assert got is not None
    assert got.access_token == "at-1"
    assert got.refresh_token == "rt-1"
    assert got.client_secret == "sec"
    assert got.expires_at == 1000.0
    assert got.scope == "read write"
    assert got.token_endpoint == "https://as.example.com/token"


def test_missing_file_is_empty_store(tmp_path):
    store = MCPAuthStore(tmp_path / "nope.json")
    assert store.get("linear", _URL) is None
    assert store.describe("linear", _URL) is None


def test_corrupt_file_is_empty_store(tmp_path):
    p = tmp_path / "mcp-auth.json"
    p.write_text("{not json", encoding="utf-8")
    store = MCPAuthStore(p)
    assert store.get("linear", _URL) is None


def test_url_mismatch_hides_credentials(tmp_path):
    store = MCPAuthStore(tmp_path / "mcp-auth.json")
    store.put("linear", _record())
    assert store.get("linear", "https://evil.example.com/mcp") is None
    assert store.describe("linear", "https://evil.example.com/mcp") is None


def test_url_trailing_slash_and_host_case_do_not_matter(tmp_path):
    store = MCPAuthStore(tmp_path / "mcp-auth.json")
    store.put("linear", _record())
    assert store.get("linear", "https://MCP.Example.com/mcp/") is not None


def test_get_requires_a_non_empty_access_token(tmp_path):
    p = tmp_path / "mcp-auth.json"
    p.write_text(json.dumps({
        "version": 1,
        "servers": {"linear": {"server_url": _URL, "client": {}, "tokens": {}}},
    }), encoding="utf-8")
    store = MCPAuthStore(p)
    assert store.get("linear", _URL) is None


def test_put_replaces_an_existing_entry(tmp_path):
    store = MCPAuthStore(tmp_path / "mcp-auth.json")
    store.put("linear", _record())
    store.put("linear", _record(access_token="at-2", refresh_token=None))
    got = store.get("linear", _URL)
    assert got is not None and got.access_token == "at-2" and got.refresh_token is None


def test_delete_removes_only_that_server(tmp_path):
    store = MCPAuthStore(tmp_path / "mcp-auth.json")
    store.put("a", _record())
    store.put("b", _record())
    assert store.delete("a") is True
    assert store.get("a", _URL) is None
    assert store.get("b", _URL) is not None
    assert store.delete("a") is False


def test_describe_has_no_secrets(tmp_path):
    store = MCPAuthStore(tmp_path / "mcp-auth.json")
    store.put("linear", _record())
    described = store.describe("linear", _URL)
    assert described == {
        "authenticated": True,
        "expires_at": 1000.0,
        "has_refresh": True,
        "client_id": "cid",
        "scope": "read write",
    }
    blob = json.dumps(described)
    assert "at-1" not in blob and "rt-1" not in blob and "sec" not in blob


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
def test_file_mode_is_0600(tmp_path):
    p = tmp_path / "mcp-auth.json"
    store = MCPAuthStore(p)
    store.put("linear", _record())
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_write_leaves_no_temp_file_behind(tmp_path):
    d = tmp_path / "state"
    store = MCPAuthStore(d / "mcp-auth.json")
    store.put("linear", _record())
    assert [f.name for f in d.iterdir()] == ["mcp-auth.json"]


def test_a_long_lived_instance_sees_another_instances_write(tmp_path):
    """The JSON-RPC auth thread / a CLI run writes through its OWN instance while
    the one held by a live MCP client keeps running — the reader must observe it."""
    p = tmp_path / "mcp-auth.json"
    reader = MCPAuthStore(p)
    assert reader.get("linear", _URL) is None
    MCPAuthStore(p).put("linear", _record())
    assert reader.get("linear", _URL) is not None


def test_a_long_lived_instance_sees_another_instances_delete(tmp_path):
    p = tmp_path / "mcp-auth.json"
    MCPAuthStore(p).put("linear", _record())
    reader = MCPAuthStore(p)
    assert reader.get("linear", _URL) is not None
    MCPAuthStore(p).delete("linear")
    assert reader.get("linear", _URL) is None


def test_a_corrupt_entry_is_dropped_on_the_next_rewrite(tmp_path):
    p = tmp_path / "mcp-auth.json"
    p.write_text(json.dumps({"version": 1, "servers": {
        "bad": "not-an-object",
        "partial": {"server_url": _URL, "client": {}, "tokens": {}},
        "good": _record().to_json(),
    }}), encoding="utf-8")
    store = MCPAuthStore(p)
    assert store.get("good", _URL) is not None
    store.put("other", _record())
    saved = json.loads(p.read_text("utf-8"))
    assert set(saved["servers"]) == {"good", "other"}


def test_is_fresh_uses_skew():
    rec = _record(expires_at=1000.0)
    assert rec.is_fresh(now=900.0, skew=30.0) is True
    assert rec.is_fresh(now=980.0, skew=30.0) is False  # inside the skew window
    assert _record(expires_at=None).is_fresh(now=0.0, skew=30.0) is False


def test_normalize_url():
    assert normalize_url("https://A.example.com/mcp/") == "https://a.example.com/mcp"
    assert normalize_url("https://a.example.com") == "https://a.example.com"


def test_same_origin():
    assert same_origin("https://a.example.com/x", "https://a.example.com/y") is True
    assert same_origin("https://a.example.com/x", "https://b.example.com/x") is False
    assert same_origin("https://a.example.com/x", "http://a.example.com/x") is False
    assert same_origin("https://a.example.com:8443/x", "https://a.example.com/x") is False


def test_default_path_follows_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert default_path() == tmp_path / ".cluxmate" / "mcp-auth.json"


def test_a_same_size_rewrite_is_observed(tmp_path):
    """The case that rules out (mtime, size) as the change signal: a re-issued
    token of the same length replaces the old one, so neither the clock nor the
    size moves. Windows' ~15.6 ms file-clock tick makes this reachable in
    practice, not just in theory."""
    p = tmp_path / "mcp-auth.json"
    MCPAuthStore(p).put("linear", _record(access_token="at-1"))
    reader = MCPAuthStore(p)
    assert reader.get("linear", _URL).access_token == "at-1"
    MCPAuthStore(p).put("linear", _record(access_token="at-2"))
    assert reader.get("linear", _URL).access_token == "at-2"
