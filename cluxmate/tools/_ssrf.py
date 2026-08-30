"""SSRF guard — URL validation for web_fetch/web_search.

An SSRF (Server-Side Request Forgery) guard validates the *destination* of an
outbound request before it is made. web_fetch/web_search run in the agent's
process at normal network privileges; a prompt-injected model could be steered
into fetching internal services (loopback dev servers, RFC1918 hosts, cloud
metadata at 169.254.169.254). This module is the T1-class containment for URL
*values*: it cannot stop malicious code, it constrains what the model supplies
(same trust model as WriteFence, docs/plans/sandbox-threat-model.md).

Design (docs/superpowers/specs/2026-08-30-ssrf-guard-design.md):
- Default-denied ranges are hardcoded and cannot be removed; users may only
  ADD ranges (block_extra) or ALLOW specific hosts/ports/CIDRs (allow).
- allow wins over every block (the config lives in ~/.cluxmate/, outside the
  model's writable roots).
- hostnames are resolved and EVERY A/AAAA address is checked; a public name
  resolving to 127.0.0.1 is blocked; resolution failure is fail-closed.
- Callers must re-validate every hop of a redirect chain (the event_hooks
  wiring in web_fetch.py does this).
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

# Default-denied networks: private / loopback / link-local / cloud metadata /
# CGNAT / multicast / reserved / IPv6 ULA + link-local + multicast / NAT64.
DEFAULT_BLOCKED_NETS = [
    ipaddress.ip_network(n) for n in (
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
        "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16",
        "198.18.0.0/15", "224.0.0.0/4", "240.0.0.0/4",
        "::/128", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8", "64:ff9b::/96",
    )
]

_SCHEMES = ("http", "https")


class SSRFBlockedError(Exception):
    """Raised by the request hook when a URL must not be fetched."""


@dataclass(frozen=True)
class _Entry:
    """One allow/block rule: CIDR, IP literal, or hostname[:port]."""
    kind: str            # "network" | "ip" | "host"
    value: object        # ip_network | ip_address | str (lowercased host)
    port: int | None     # None = any port


def _parse_port(text: str) -> int | None:
    """Parse ':3000' / '3000' → 3000, or None if missing/invalid."""
    if not text:
        return None
    t = text[1:] if text[0] == ":" else text
    if not t.isdigit():
        return None
    port = int(t)
    return port if 1 <= port <= 65535 else None


def parse_entry(entry: str) -> _Entry | None:
    """Parse an allow/block entry. Returns None when the entry is invalid.

    Accepted forms: host, host:port, [ipv6]:port, bare ipv4/ipv6, CIDR.
    """
    raw = (entry or "").strip()
    if not raw:
        return None
    low = raw.lower()
    if any(ch.isspace() for ch in low):
        return None
    # [ipv6]:port (or bare [ipv6])
    if low.startswith("["):
        close = low.find("]")
        if close == -1:
            return None
        host_part = low[1:close]
        port = _parse_port(low[close + 1:])
        if port is None and low[close + 1:] != "":
            return None
        try:
            return _Entry("ip", ipaddress.ip_address(host_part), port)
        except ValueError:
            return None
    # bare IP literal (IPv4 or IPv6, no port)
    try:
        return _Entry("ip", ipaddress.ip_address(low), None)
    except ValueError:
        pass
    # CIDR (must contain '/')
    if "/" in low:
        try:
            return _Entry("network", ipaddress.ip_network(low, strict=False), None)
        except ValueError:
            return None
    # hostname[:port] — an IP-literal host part stays an "ip" entry so it also
    # matches hostnames that RESOLVE to that IP (e.g. allow "127.0.0.1:3000"
    # + request "localhost:3000").
    if ":" in low:
        host_part, _, port_part = low.rpartition(":")
        port = _parse_port(port_part)
        if port is not None and host_part and ":" not in host_part:
            try:
                return _Entry("ip", ipaddress.ip_address(host_part), port)
            except ValueError:
                return _Entry("host", host_part, port)
        return None
    return _Entry("host", low, None)


def _resolve_ips(host: str) -> list | None:
    """Resolve *host* to all IPs. Returns None on resolution failure (fail-closed)."""
    try:
        addr = ipaddress.ip_address(host)  # host is already an IP literal
        out = [addr]
        # IPv4-mapped IPv6 (::ffff:127.0.0.1) also matches its IPv4 form, so
        # the check sees 127.0.0.1 against the 127.0.0.0/8 default deny
        # (same expansion as the getaddrinfo branch below).
        mapped = getattr(addr, "ipv4_mapped", None)
        if mapped is not None:
            out.append(mapped)
        return out
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return None
    out: list = []
    seen: set[str] = set()
    for info in infos:
        ip = info[4][0]
        if ip in seen:
            continue
        seen.add(ip)
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        out.append(addr)
        # IPv4-mapped IPv6 (::ffff:127.0.0.1) also matches its IPv4 form, so
        # the check sees 127.0.0.1 against the 127.0.0.0/8 default deny.
        mapped = getattr(addr, "ipv4_mapped", None)
        if mapped is not None:
            out.append(mapped)
    return out


def _effective_port(scheme: str, port: int | None) -> int:
    if port:
        return port
    return 443 if scheme == "https" else 80


def _entry_matches(entry: _Entry, host: str, port: int, ips: list) -> bool:
    """True when the request (host, port, resolved ips) satisfies *entry*."""
    if entry.port is not None and entry.port != port:
        return False
    if entry.kind == "host":
        return entry.value == host
    if entry.kind == "ip":
        return any(ip == entry.value for ip in ips)
    return any(ip in entry.value for ip in ips)  # network


def _blocked_message(url: str, host: str, ips: list) -> str:
    ip_txt = ", ".join(str(ip) for ip in ips)
    return (
        f"SSRF guard blocked request to '{url}' — target resolves to an "
        f"internal/private address ({ip_txt}). To allow it, add "
        f"'{host}' in Settings → Sandbox → Network access"
    )


def validate_url(
    url: str,
    allow: list[str] | None = None,
    block_extra: list[str] | None = None,
) -> str | None:
    """Validate *url* against the SSRF policy.

    Returns an error message when the URL must be blocked, else None.
    ``allow`` and ``block_extra`` are raw entry strings (host / host:port /
    [ipv6]:port / IP / CIDR). ``allow`` wins over every block; the default
    denied ranges always apply (block_extra only adds to them).
    """
    # CPython raises ValueError for malformed bracket-IPv6 netlocs both eagerly
    # in urlsplit() and lazily in .hostname/.port — catch all three so a bad
    # URL returns an error message instead of aborting the request ungracefully.
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        port = _effective_port(scheme, parsed.port)
    except ValueError:
        return f"Invalid URL: {url}"
    if scheme not in _SCHEMES:
        return f"Blocked scheme '{parsed.scheme}': only http/https are allowed"
    if not host:
        return f"URL has no host: {url}"

    ips = _resolve_ips(host)
    if ips is None:
        return (
            f"SSRF guard blocked request to '{url}' — DNS resolution failed "
            f"(fail-closed). To allow it, add '{host}' in Settings → Sandbox → "
            f"Network access"
        )

    for e in (parse_entry(a) for a in (allow or [])):
        if e is not None and _entry_matches(e, host, port, ips):
            return None  # allow wins over every block

    if any(any(ip in net for ip in ips) for net in DEFAULT_BLOCKED_NETS):
        return _blocked_message(url, host, ips)
    for e in (parse_entry(b) for b in (block_extra or [])):
        if e is not None and _entry_matches(e, host, port, ips):
            return _blocked_message(url, host, ips)
    return None
