"""Minimal MCP stdio server for tests.

Speaks newline-delimited JSON-RPC just well enough to exercise MCPManager:
- initialize handshake
- notifications/initialized (ack only, no response)
- tools/list (returns one tool: echo)
- tools/call (echoes the input text back)

Run as: python tests/core/fake_mcp_server.py
"""
import json
import sys


def handle_request(req: dict) -> dict | None:
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params", {}) or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-mcp", "version": "0.1"},
            },
        }
    if method == "tools/list":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "tools": [{
                    "name": "echo",
                    "description": "Echo back the input text.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                }]
            },
        }
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments", {}) or {}
        if name == "echo":
            text = args.get("text", "")
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "isError": False,
                },
            }
        return {
            "jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"Unknown tool: {name}"},
        }
    # notifications/initialized and anything else: no id → no response.
    return None


def main() -> None:
    # readline() loop (not `for line in sys.stdin`) — the iterator over stdin
    # does read-ahead buffering that stalls on small payloads in a pipe.
    while True:
        line = sys.stdin.readline()
        if not line:
            break  # EOF
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle_request(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
