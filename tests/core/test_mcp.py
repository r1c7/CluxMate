"""Tests for MCPManager — config loading, server handshake, tool dispatch."""

import json
import sys
import tempfile
from pathlib import Path

import pytest

from cluxmate.core.mcp import MCPConfigManager, MCPManager

_FAKE_SERVER = Path(__file__).parent / "fake_mcp_server.py"


def _write_mcp_json(dir_path: Path, servers: dict) -> None:
    """Write <dir>/.cluxmate/mcp.json with the given mcpServers config."""
    cfg_dir = dir_path / ".cluxmate"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "mcp.json").write_text(
        json.dumps({"mcpServers": servers}, indent=2), encoding="utf-8"
    )


def _fake_server_config(extra: dict | None = None) -> dict:
    cfg = {
        "command": sys.executable,
        "args": [str(_FAKE_SERVER)],
    }
    if extra:
        cfg.update(extra)
    return cfg


def _mgr_with_home(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """Redirect ~/.cluxmate under tmp_path. Returns (home_dir, project_dir)."""
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    project = tmp_path / "project"
    project.mkdir()
    return home, project


def test_config_load_global_only(tmp_path, monkeypatch):
    home, project = _mgr_with_home(tmp_path, monkeypatch)
    _write_mcp_json(home, {"fake": _fake_server_config()})
    configs = MCPConfigManager(str(project)).load()
    assert "fake" in configs
    assert configs["fake"].transport == "stdio"
    assert configs["fake"].command == sys.executable


def test_config_project_overrides_global(tmp_path, monkeypatch):
    home, project = _mgr_with_home(tmp_path, monkeypatch)
    # Global: a fake-server config.
    _write_mcp_json(home, {"fake": _fake_server_config({"call_timeout_s": 30})})
    # Project: only overrides `disabled`. Deep-merge should keep the command.
    _write_mcp_json(project, {"fake": {"disabled": True}})

    configs = MCPConfigManager(str(project)).load()
    assert configs["fake"].disabled is True
    # Command from global survives the merge.
    assert configs["fake"].command == sys.executable
    # And the timeout override from global survives.
    assert configs["fake"].call_timeout_s == 30


def test_config_authorization_env_injects_bearer(tmp_path, monkeypatch):
    home, project = _mgr_with_home(tmp_path, monkeypatch)
    _write_mcp_json(home, {
        "remote": {
            "url": "https://example.invalid/mcp",
            "authorization_env": "FAKE_MCP_TOKEN",
        }
    })
    monkeypatch.setenv("FAKE_MCP_TOKEN", "secret-token-123")
    configs = MCPConfigManager(str(project)).load()
    assert configs["remote"].transport == "http"
    assert configs["remote"].headers["Authorization"] == "Bearer secret-token-123"


def test_config_expands_env_vars(tmp_path, monkeypatch):
    home, project = _mgr_with_home(tmp_path, monkeypatch)
    _write_mcp_json(home, {
        "pg": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-postgres", "${PG_DSN}"],
            "env": {"TOKEN": "${PG_SECRET}"},
        }
    })
    monkeypatch.setenv("PG_DSN", "postgresql://u:p@host:5432/db")
    monkeypatch.setenv("PG_SECRET", "s3cr3t")
    configs = MCPConfigManager(str(project)).load()
    assert configs["pg"].args[-1] == "postgresql://u:p@host:5432/db"
    assert "${" not in configs["pg"].args[-1]
    assert configs["pg"].env["TOKEN"] == "s3cr3t"


def test_config_unknown_env_var_expands_empty(tmp_path, monkeypatch):
    home, project = _mgr_with_home(tmp_path, monkeypatch)
    _write_mcp_json(home, {
        "remote": {"url": "https://example.invalid/${MISSING_VAR}/mcp"}
    })
    monkeypatch.delenv("MISSING_VAR", raising=False)
    configs = MCPConfigManager(str(project)).load()
    # Unknown var → "" (shell/envsubst semantics), no literal ${...} left.
    assert configs["remote"].url == "https://example.invalid//mcp"


def test_config_rejects_invalid_server_name(tmp_path, monkeypatch):
    home, project = _mgr_with_home(tmp_path, monkeypatch)
    _write_mcp_json(home, {
        "has space": _fake_server_config(),
        "ok-name": _fake_server_config(),
    })
    configs = MCPConfigManager(str(project)).load()
    assert "has space" not in configs
    assert "ok-name" in configs


@pytest.mark.asyncio
async def test_mcp_load_and_call_echo(tmp_path, monkeypatch):
    home, project = _mgr_with_home(tmp_path, monkeypatch)
    _write_mcp_json(project, {"fake": _fake_server_config()})

    mgr = MCPManager(str(project))
    try:
        mgr.load()
        tools = mgr.list_tools()
        assert len(tools) == 1
        wrapper = tools[0]
        assert wrapper.name == "mcp__fake__echo"
        assert wrapper.risk_level == "write"

        result = await wrapper.execute(text="hello world")
        assert result == "hello world"
    finally:
        mgr.shutdown()


def test_mcp_disabled_server_excluded(tmp_path, monkeypatch):
    home, project = _mgr_with_home(tmp_path, monkeypatch)
    _write_mcp_json(project, {"fake": _fake_server_config({"disabled": True})})

    mgr = MCPManager(str(project))
    try:
        mgr.load()
        # No tools surfaced for a disabled server.
        assert mgr.list_tools() == []
        statuses = {s["name"]: s for s in mgr.status()}
        assert statuses["fake"]["disabled"] is True
        assert statuses["fake"]["status"] == "disabled"
    finally:
        mgr.shutdown()


def test_mcp_status_reports_connected_after_handshake(tmp_path, monkeypatch):
    home, project = _mgr_with_home(tmp_path, monkeypatch)
    _write_mcp_json(project, {"fake": _fake_server_config()})

    mgr = MCPManager(str(project))
    try:
        mgr.load()
        statuses = {s["name"]: s for s in mgr.status()}
        assert statuses["fake"]["status"] == "connected"
        assert len(statuses["fake"]["tools"]) == 1
        assert statuses["fake"]["tools"][0]["name"] == "echo"
    finally:
        mgr.shutdown()


def test_mcp_load_is_idempotent(tmp_path, monkeypatch):
    """load() twice should not re-spawn subprocesses or duplicate tools."""
    home, project = _mgr_with_home(tmp_path, monkeypatch)
    _write_mcp_json(project, {"fake": _fake_server_config()})

    mgr = MCPManager(str(project))
    try:
        mgr.load()
        tools1 = mgr.list_tools()
        mgr.load()  # second call — should be a no-op
        tools2 = mgr.list_tools()
        assert len(tools1) == len(tools2) == 1
        assert tools1[0].name == tools2[0].name
    finally:
        mgr.shutdown()


# ── OAuth config surface (no network) ──────────────────────────────

from cluxmate.core.mcp_oauth import OAuthFlowConfig


def test_remote_without_auth_gets_latent_oauth(tmp_path, monkeypatch):
    home, project = _mgr_with_home(tmp_path, monkeypatch)
    _write_mcp_json(home, {"remote": {"url": "https://example.invalid/mcp"}})
    cfg = MCPConfigManager(str(project)).load()["remote"]
    assert isinstance(cfg.oauth, OAuthFlowConfig)
    assert cfg.oauth.client_id is None
    assert cfg.oauth.server_url == "https://example.invalid/mcp"
    assert cfg.oauth_error is None


def test_explicit_oauth_object_is_parsed(tmp_path, monkeypatch):
    home, project = _mgr_with_home(tmp_path, monkeypatch)
    _write_mcp_json(home, {"remote": {
        "url": "https://example.invalid/mcp",
        "oauth": {"client_id": "cid", "client_secret_env": "MCP_SECRET",
                  "scopes": "read write", "callback_port": 19876},
    }})
    monkeypatch.setenv("MCP_SECRET", "s3cret")
    cfg = MCPConfigManager(str(project)).load()["remote"]
    assert cfg.oauth.client_id == "cid"
    assert cfg.oauth.client_secret == "s3cret"
    assert cfg.oauth.scopes == "read write"
    assert cfg.oauth.callback_port == 19876


def test_missing_client_secret_env_yields_none(tmp_path, monkeypatch):
    home, project = _mgr_with_home(tmp_path, monkeypatch)
    _write_mcp_json(home, {"remote": {
        "url": "https://example.invalid/mcp",
        "oauth": {"client_id": "cid", "client_secret_env": "MCP_ABSENT"},
    }})
    monkeypatch.delenv("MCP_ABSENT", raising=False)
    assert MCPConfigManager(str(project)).load()["remote"].oauth.client_secret is None


def test_oauth_false_disables_it(tmp_path, monkeypatch):
    home, project = _mgr_with_home(tmp_path, monkeypatch)
    _write_mcp_json(home, {"remote": {
        "url": "https://example.invalid/mcp", "oauth": False}})
    assert MCPConfigManager(str(project)).load()["remote"].oauth is None


def test_static_authorization_suppresses_oauth(tmp_path, monkeypatch):
    home, project = _mgr_with_home(tmp_path, monkeypatch)
    _write_mcp_json(home, {"remote": {
        "url": "https://example.invalid/mcp",
        "headers": {"Authorization": "Bearer static-token"}}})
    assert MCPConfigManager(str(project)).load()["remote"].oauth is None


def test_static_authorization_env_suppresses_oauth(tmp_path, monkeypatch):
    home, project = _mgr_with_home(tmp_path, monkeypatch)
    _write_mcp_json(home, {"remote": {
        "url": "https://example.invalid/mcp", "authorization_env": "MCP_TOKEN"}})
    monkeypatch.setenv("MCP_TOKEN", "static-token")
    assert MCPConfigManager(str(project)).load()["remote"].oauth is None


def test_explicit_oauth_beats_static_authorization(tmp_path, monkeypatch):
    home, project = _mgr_with_home(tmp_path, monkeypatch)
    _write_mcp_json(home, {"remote": {
        "url": "https://example.invalid/mcp",
        "headers": {"Authorization": "Bearer static-token"},
        "oauth": {"client_id": "cid"}}})
    cfg = MCPConfigManager(str(project)).load()["remote"]
    assert isinstance(cfg.oauth, OAuthFlowConfig)
    assert cfg.oauth.client_id == "cid"
    # The static header is still there — the client reports the conflict.
    assert cfg.headers["Authorization"] == "Bearer static-token"


def test_stdio_with_oauth_is_a_config_error(tmp_path, monkeypatch):
    home, project = _mgr_with_home(tmp_path, monkeypatch)
    _write_mcp_json(home, {"fake": _fake_server_config({"oauth": {"client_id": "x"}})})
    cfg = MCPConfigManager(str(project)).load()["fake"]
    assert cfg.oauth is None
    assert cfg.oauth_error == "oauth is only supported for remote (url) MCP servers"


def test_stdio_without_oauth_has_no_error(tmp_path, monkeypatch):
    home, project = _mgr_with_home(tmp_path, monkeypatch)
    _write_mcp_json(home, {"fake": _fake_server_config()})
    cfg = MCPConfigManager(str(project)).load()["fake"]
    assert cfg.oauth is None and cfg.oauth_error is None
