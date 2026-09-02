"""Local allowlist-filtering HTTP proxy — enforces the egress proxy allowlist.

proxy mode is best-effort by design: only clients that honor HTTP_PROXY /
HTTPS_PROXY / ALL_PROXY route through this loopback proxy. It handles HTTP
absolute-form requests (forwarding the origin-form upstream) and CONNECT
tunnels (for HTTPS). The allowlist is a full egress whitelist — a host not in
the list is denied, public or not.
"""

from __future__ import annotations

import http.client
import http.server
import select
import socket
import socketserver
import threading
import urllib.parse

from cluxmate.tools._ssrf import _entry_matches, _idna_encode, _resolve_ips, parse_entry


def allowlist_matches(host: str, port: int, allow: list[str]) -> bool:
    """True when (host, port) is allowed by the allow entries.

    Reuses the SSRF parser/resolver so allow entries accept the same forms
    (host / host:port / [ipv6]:port / IP / CIDR). Unlike the SSRF guard there
    is NO default-denied table — an empty allowlist blocks everything.
    """
    if not allow:
        return False
    host = _idna_encode(host)
    ips = _resolve_ips(host)
    if ips is None:
        return False
    for raw in allow:
        e = parse_entry(raw)
        if e is not None and _entry_matches(e, host, port, ips):
            return True
    return False


class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _deny(self):
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _target(self):
        raw = self.path
        if not raw.startswith(("http://", "https://")):
            return None
        parts = urllib.parse.urlsplit(raw)
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return host, port, raw

    def _forward(self, method: str):
        t = self._target()
        if t is None:
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        host, port, full_url = t
        if not allowlist_matches(host, port, self.server.allow):
            self._deny()
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else None
        parts = urllib.parse.urlsplit(full_url)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        skip = {"host", "proxy-connection", "connection", "keep-alive",
                "transfer-encoding", "content-length"}
        headers = {k: v for k, v in self.headers.items() if k.lower() not in skip}
        conn = http.client.HTTPConnection(host, port, timeout=30)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            self.send_response(resp.status, resp.reason)
            for k, v in resp.getheaders():
                if k.lower() not in ("connection", "transfer-encoding", "content-length"):
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            try:
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except OSError:
                pass
        finally:
            conn.close()

    def do_GET(self):
        self._forward("GET")

    def do_POST(self):
        self._forward("POST")

    def do_PUT(self):
        self._forward("PUT")

    def do_DELETE(self):
        self._forward("DELETE")

    def do_CONNECT(self):
        host, _, port_s = self.path.rpartition(":")
        port = int(port_s) if port_s.isdigit() else 443
        if not allowlist_matches(host, port, self.server.allow):
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            upstream = socket.create_connection((host, port), timeout=30)
        except OSError:
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200, "Connection Established")
        self.send_header("Content-Length", "0")
        self.end_headers()
        self._pump(self.connection, upstream)
        upstream.close()

    def _pump(self, a, b):
        try:
            closed = set()
            while True:
                readable = [s for s in (a, b) if s not in closed]
                if not readable:
                    return
                r, _, _ = select.select(readable, [], [], 30)
                if not r:
                    return
                for s in r:
                    data = s.recv(65536)
                    peer = b if s is a else a
                    if not data:
                        closed.add(s)
                        try:
                            peer.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
                    else:
                        peer.sendall(data)
        except OSError:
            pass


class _ProxyServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, allow):
        self.allow = allow
        super().__init__(addr, _ProxyHandler)


class LocalFilteringProxy:
    """A loopback HTTP proxy that enforces an allowlist (full whitelist)."""

    def __init__(self, allow: list[str] | None = None):
        self._allow = list(allow or [])
        self._server: _ProxyServer | None = None
        self._thread: threading.Thread | None = None
        self.addr: tuple[str, int] | None = None

    def start(self) -> tuple[str, int]:
        if self._server is not None:
            return self.addr
        srv = _ProxyServer(("127.0.0.1", 0), self._allow)
        self._server = srv
        self._thread = threading.Thread(target=srv.serve_forever, daemon=True)
        self._thread.start()
        self.addr = srv.server_address
        return self.addr

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            self._thread = None
            self.addr = None
