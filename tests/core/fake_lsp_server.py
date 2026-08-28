"""Minimal LSP stdio server for tests.

Speaks newline-delimited JSON-RPC just well enough to exercise LSPClient:
- initialize (returns utf-16 positionEncoding)
- initialized (ack only)
- textDocument/definition (returns one fixed location)
- textDocument/references / hover / documentSymbol / workspace/symbol

Run as: python tests/core/fake_lsp_server.py
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
                "capabilities": {"positionEncoding": "utf-16"},
            },
        }
    if method == "shutdown":
        return {"jsonrpc": "2.0", "id": req_id, "result": None}
    if method == "textDocument/definition":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": [{
                "uri": "file:///fake/def.py",
                "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 4}},
            }],
        }
    if method == "textDocument/references":
        return {"jsonrpc": "2.0", "id": req_id, "result": []}
    if method == "textDocument/hover":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {"contents": {"kind": "markdown", "value": "```python\ndef foo()\n```"}},
        }
    if method == "textDocument/documentSymbol":
        return {"jsonrpc": "2.0", "id": req_id, "result": []}
    if method == "workspace/symbol":
        return {"jsonrpc": "2.0", "id": req_id, "result": []}
    # notifications (initialized, didOpen, didChange, exit) have no id.
    return None


def main() -> None:
    while True:
        line = sys.stdin.readline()
        if not line:
            break
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
