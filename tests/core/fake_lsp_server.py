"""Minimal LSP stdio server for tests.

Speaks the real LSP Base Protocol over stdio — Content-Length framed JSON,
NOT newline-delimited JSON — just well enough to exercise LSPClient:
- initialize (returns utf-16 positionEncoding)
- initialized (ack only)
- textDocument/definition (returns one fixed location)
- textDocument/references / hover / documentSymbol / workspace/symbol

Run as: python tests/core/fake_lsp_server.py
"""
import json
import sys


def read_message(stream) -> dict | None:
    """Read one Content-Length framed message; None on EOF."""
    content_length = None
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.rstrip(b"\r\n")
        if line == b"":
            break
        if line.lower().startswith(b"content-length:"):
            content_length = int(line.split(b":", 1)[1].strip())
    if content_length is None:
        return None
    body = stream.read(content_length)
    if not body:
        return None
    return json.loads(body.decode("utf-8"))


def write_message(stream, payload: dict) -> None:
    """Write one Content-Length framed message (body has no trailing newline)."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    stream.flush()


def handle_request(req: dict, stream) -> dict | None:
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params", {}) or {}

    if method == "initialize":
        # Real servers issue a server→client request mid-handshake and BLOCK
        # until the client answers. A client that drops it deadlocks. We send
        # workspace/configuration (server id space starts at 1, deliberately
        # colliding with the client's) and only reply to initialize once the
        # client has answered it — so start() hangs unless the client responds.
        write_message(stream, {
            "jsonrpc": "2.0", "id": 1, "method": "workspace/configuration",
            "params": {"items": [{"section": "python"}]},
        })
        reply = read_message(sys.stdin.buffer)
        if reply is None or reply.get("id") != 1 or "result" not in reply:
            return None  # client failed to answer → let it observe EOF/hang
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
        req = read_message(sys.stdin.buffer)
        if req is None:
            break
        resp = handle_request(req, sys.stdout.buffer)
        if resp is not None:
            write_message(sys.stdout.buffer, resp)


if __name__ == "__main__":
    main()
