"""Tests for MCP stdio-server sandboxing (phase 1 extension).

Windows-only end-to-end: spawn a low-IL child, verify NO_WRITE_UP denies
home writes while pipe JSON-RPC works; then drive a real MCP handshake
through MCPManager with the sandbox attached.
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from cluxmate.core.mcp import MCPClient, MCPConfig, MCPManager
from cluxmate.tools._sandbox import WindowsLowILSandbox

sys.stdout.reconfigure(errors="replace")

IS_WIN = sys.platform == "win32"

# A minimal stdio MCP server that answers initialize / tools/list / tools/call.
_MCP_SERVER = r'''
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except Exception:
        continue
    mid = req.get("id")
    method = req.get("method")
    if method == "initialize":
        out = {"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":"2024-11-05","capabilities":{},"serverInfo":{"name":"fake","version":"1"}}}
    elif method == "tools/list":
        out = {"jsonrpc":"2.0","id":mid,"result":{"tools":[{"name":"echo","description":"echo","inputSchema":{"type":"object","properties":{}}}]}}
    elif method == "tools/call":
        out = {"jsonrpc":"2.0","id":mid,"result":{"content":[{"type":"text","text":"ECHO:"+str(req["params"].get("arguments",{}).get("x",""))}]}}
    else:
        out = {"jsonrpc":"2.0","id":mid,"result":{}}
    print(json.dumps(out)); sys.stdout.flush()
'''


@pytest.mark.skipif(not IS_WIN, reason="windows-only")
def test_spawn_popen_child_is_low_il():
    ws = Path(tempfile.mkdtemp(prefix="cluxmate-mcp-"))
    try:
        sb = WindowsLowILSandbox(str(ws))
        escape = Path.home() / "cluxmate-mcp-escape.txt"
        code = (
            "import os\n"
            f"try:\n"
            f"    open({str(escape)!r}, 'w').write('x')\n"
            f"    print('ESCAPED'); \n"
            f"except OSError:\n"
            f"    print('DENIED')\n"
            "import sys; sys.stdout.flush()\n"
        )
        p = sb.spawn_popen(
            [sys.executable, "-u", "-c", code],
            cwd=str(ws), env=os.environ.copy(),
        )
        out = p.stdout.readline().strip()
        p.wait(timeout=30)
        assert out == "DENIED"
        assert not escape.exists()
    finally:
        shutil.rmtree(ws, ignore_errors=True)


@pytest.mark.skipif(not IS_WIN, reason="windows-only")
def test_mcp_manager_load_handshake_under_sandbox():
    ws = Path(tempfile.mkdtemp(prefix="cluxmate-mcp-"))
    try:
        # Write the fake server + a real project mcp.json, then drive the full
        # MCPManager.load() discovery → sandbox spawn → handshake path.
        server_path = ws / "fake_server.py"
        server_path.write_text(_MCP_SERVER, encoding="utf-8")
        (ws / ".cluxmate").mkdir(parents=True, exist_ok=True)
        (ws / ".cluxmate" / "mcp.json").write_text(json.dumps({
            "mcpServers": {
                "fake": {
                    "command": sys.executable,
                    "args": ["-u", str(server_path)],
                    "risk_level": "safe",
                }
            }
        }), encoding="utf-8")

        sb = WindowsLowILSandbox(str(ws))
        mgr = MCPManager(str(ws), sandbox=sb)
        mgr.load()
        try:
            statuses = {s["name"]: s for s in mgr.status()}
            assert statuses["fake"]["status"] == "connected"
            tools = mgr.list_tools()
            assert any(t.name == "mcp__fake__echo" for t in tools)
            echo = next(t for t in tools if t.name == "mcp__fake__echo")
            import asyncio
            result = asyncio.run(echo.execute(x="hello"))
            assert "ECHO:hello" in result
        finally:
            mgr.shutdown()
    finally:
        shutil.rmtree(ws, ignore_errors=True)


@pytest.mark.skipif(not IS_WIN, reason="windows-only")
def test_mcp_unsandboxed_fallback_when_no_sandbox():
    # sandbox=None → bare Popen path still works (best-effort semantics).
    ws = Path(tempfile.mkdtemp(prefix="cluxmate-mcp-"))
    try:
        server_path = ws / "fake_server.py"
        server_path.write_text(_MCP_SERVER, encoding="utf-8")
        cfg = MCPConfig(
            name="fake", transport="stdio",
            command=sys.executable, args=["-u", str(server_path)],
        )
        from cluxmate.core.mcp import MCPClient
        client = MCPClient(cfg, sandbox=None, cwd=str(ws))
        assert client.start() is True
        assert client._status == "connected"
        client.shutdown()
    finally:
        shutil.rmtree(ws, ignore_errors=True)
