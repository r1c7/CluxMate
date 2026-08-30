"""Tests for the SSRF guard core (_ssrf.py). No network: loopback literals and
hosts-file resolution only; DNS-dependent cases monkeypatch getaddrinfo."""

import socket

from cluxmate.tools._ssrf import validate_url


def allowed(url, allow=(), block_extra=()):
    return validate_url(url, list(allow), list(block_extra)) is None


def blocked(url, allow=(), block_extra=()):
    return validate_url(url, list(allow), list(block_extra)) is not None


# ── scheme ────────────────────────────────────────────────────────────
def test_non_http_schemes_blocked():
    assert blocked("file:///etc/passwd")
    assert blocked("gopher://127.0.0.1/")
    assert blocked("ftp://10.0.0.1/")


# ── default blocked nets (literal IPs) ────────────────────────────────
def test_default_blocked_literal_ips():
    for ip in ("10.0.0.1", "127.0.0.1", "192.168.1.1", "172.16.0.1",
               "169.254.169.254", "0.0.0.1", "100.64.0.1", "198.18.0.1",
               "224.0.0.1", "240.0.0.1"):
        assert blocked(f"http://{ip}/")


def test_ipv6_literals_blocked():
    assert blocked("http://[::1]:8080/")
    assert blocked("http://[fc00::1]/")
    assert blocked("http://[fe80::1]/")


def test_ipv4_mapped_ipv6_blocked():
    assert blocked("http://[::ffff:127.0.0.1]:80/")


def test_public_ip_allowed():
    assert allowed("http://8.8.8.8/")


# ── hostname resolution ───────────────────────────────────────────────
def test_localhost_resolves_internal():
    # hosts-file resolution, no network
    assert blocked("http://localhost:8080/")


def test_missing_host():
    assert blocked("http:///path")


def test_dns_failure_fails_closed(monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror("no such host")
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert blocked("http://example.invalid/")


# ── allow matching ────────────────────────────────────────────────────
def test_allow_host_matches_any_port():
    assert allowed("http://localhost:8080/", allow=["localhost"])


def test_allow_host_requires_port_match():
    assert blocked("http://localhost:3001/", allow=["localhost:3000"])


def test_allow_ip_matches_hostname_via_dns():
    assert allowed("http://localhost:3000/", allow=["127.0.0.1:3000"])


def test_allow_cidr():
    assert allowed("http://127.0.0.1:8080/", allow=["127.0.0.0/8"])


def test_allow_case_insensitive():
    assert allowed("http://localhost:3000/", allow=["LOCALHOST:3000"])


def test_allow_does_not_open_everything():
    assert blocked("http://10.0.0.1/", allow=["8.8.8.8"])


def test_allow_wins_over_default_block():
    assert allowed("http://127.0.0.1:9000/", allow=["127.0.0.1"])


# ── block_extra ───────────────────────────────────────────────────────
def test_block_extra_cidr_blocks():
    assert blocked("http://203.0.113.9/", block_extra=["203.0.113.0/24"])


def test_block_extra_absent_means_public():
    assert allowed("http://203.0.113.9/")


def test_allow_wins_over_block_extra():
    assert allowed(
        "http://203.0.113.5/",
        allow=["203.0.113.5"],
        block_extra=["203.0.113.0/24"],
    )


def test_malformed_bracket_returns_message():
    err = validate_url("http://[::1:80/")
    assert isinstance(err, str) and err


# ── I1: IPv4-compatible IPv6 (::/96) is default-blocked ───────────────
def test_ipv4_compatible_ipv6_blocked():
    # RFC 4291 IPv4-compatible form of 127.0.0.1
    assert blocked("http://[::127.0.0.1]:6379/")


# ── I2: resolution failure is fail-closed, including OSError/empty ─────
def test_dns_empty_result_fails_closed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [])
    assert blocked("http://example.invalid/")


def test_dns_oserror_fails_closed(monkeypatch):
    def boom(*a, **k):
        raise OSError("EAI_SYSTEM")
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert blocked("http://example.invalid/")


# ── I3: IDN (non-ASCII) hostnames are IDNA-encoded like httpx ─────────
def test_idn_hostname_is_encoded(monkeypatch):
    captured = {}
    def fake_getaddrinfo(host, port, *a, **k):
        captured["host"] = host
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert allowed("http://müller.example/")
    assert captured["host"] == "xn--mller-kva.example"


def test_idn_allow_entry_matches(monkeypatch):
    # Resolve to an internal address so ONLY the IDN allow entry can rescue
    # the request (a public IP would make the test pass even if allow failed).
    def fake_getaddrinfo(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert allowed("http://müller.example/", allow=["müller.example"])
