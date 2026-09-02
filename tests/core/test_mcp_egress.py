"""Tests for MCP egress-mode passthrough and status visibility."""

from cluxmate.core.mcp import MCPClient, MCPConfig, MCPManager


def test_mcp_manager_stores_egress_mode(tmp_path):
    m = MCPManager(str(tmp_path), sandbox=None, egress_mode="off")
    assert m._egress_mode == "off"


def test_mcp_client_status_exposes_egress():
    cfg = MCPConfig(name="srv", transport="http", url="http://127.0.0.1:1")
    client = MCPClient(cfg, cwd=".", egress_mode="proxy")
    assert client.status()["egress"] == "proxy"
