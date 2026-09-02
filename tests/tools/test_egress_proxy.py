"""Tests for the local allowlist-filtering proxy (egress proxy mode)."""

import http.client
import http.server
import socket
import socketserver
import threading
import time

from cluxmate.tools._egress_proxy import LocalFilteringProxy, _ProxyHandler, allowlist_matches


class _Upstream(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _serve_upstream():
    srv = socketserver.TCPServer(("127.0.0.1", 0), _Upstream)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def test_allowlist_empty_blocks_everything():
    assert allowlist_matches("example.com", 443, []) is False


def test_allowlist_hostname_matches():
    assert allowlist_matches("localhost", 3000, ["localhost:3000"]) is True


def test_allowlist_port_mismatch_blocks():
    assert allowlist_matches("localhost", 4000, ["localhost:3000"]) is False


def test_allowlist_ip_literal_matches():
    assert allowlist_matches("127.0.0.1", 3000, ["127.0.0.1:3000"]) is True


def test_allowlist_cidr_matches():
    assert allowlist_matches("10.0.0.5", 80, ["10.0.0.0/8"]) is True


def test_proxy_allows_listed_host():
    upstream = _serve_upstream()
    try:
        host, port = upstream.server_address
        proxy = LocalFilteringProxy(allow=[f"127.0.0.1:{port}"])
        proxy.start()
        try:
            phost, pport = proxy.addr
            conn = http.client.HTTPConnection(phost, pport, timeout=10)
            conn.request("GET", f"http://127.0.0.1:{port}/hello")
            resp = conn.getresponse()
            assert resp.status == 200
            assert resp.read() == b"ok"
        finally:
            proxy.stop()
    finally:
        upstream.shutdown()
        upstream.server_close()


def test_proxy_denies_unlisted_host():
    upstream = _serve_upstream()
    try:
        proxy = LocalFilteringProxy(allow=[])
        proxy.start()
        try:
            phost, pport = proxy.addr
            conn = http.client.HTTPConnection(phost, pport, timeout=10)
            conn.request("GET", f"http://127.0.0.1:{upstream.server_address[1]}/x")
            resp = conn.getresponse()
            assert resp.status == 403
        finally:
            proxy.stop()
    finally:
        upstream.shutdown()
        upstream.server_close()


def test_pump_relays_after_peer_half_close():
    a1, a2 = socket.socketpair()
    b1, b2 = socket.socketpair()
    handler = _ProxyHandler.__new__(_ProxyHandler)

    def run_pump():
        handler._pump(a1, b1)

    t = threading.Thread(target=run_pump, daemon=True)
    t.start()
    try:
        b2.sendall(b"first-")
        a2.settimeout(5)
        first = a2.recv(len(b"first-"))
        assert first == b"first-"
        a2.shutdown(socket.SHUT_WR)

        time.sleep(0.05)
        b2.sendall(b"second")
        b2.close()

        received = bytearray(first)
        while len(received) < len(b"first-second"):
            chunk = a2.recv(4096)
            if not chunk:
                break
            received.extend(chunk)
        t.join(timeout=5)
        assert received == b"first-second"
    finally:
        a1.close()
        a2.close()
        b1.close()
        b2.close()
