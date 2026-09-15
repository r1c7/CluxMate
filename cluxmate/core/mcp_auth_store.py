"""OAuth credential store for remote MCP servers.

One JSON file at ``~/.cluxmate/mcp-auth.json`` holding, per server name, the
client registration and tokens obtained from an OAuth 2.0 authorization server.
It lives in ``~/.cluxmate/`` — user-global, and outside the WriteFence roots, so
the model can never rewrite its own credentials (the read side is closed by
``core/read_denies.py``, which adds this path unconditionally).

A record is bound to the URL it was issued for: ``get(name, url)`` returns None
when the stored ``server_url`` differs from the configured one, so pointing a
server at a new host forces re-authorization instead of leaking a token to a
host that never issued it.

Write path mirrors ``tools/_fileio.py``: sibling temp file + ``os.replace``,
with the mode forced to 0600. Reads are best-effort (a corrupt file is an empty
store). There is NO cross-process lock — concurrent writers are last-writer-wins,
the same contract as ``config.json`` / ``sandbox-grants.json``.

The URL helpers live here (not in ``mcp_oauth``) because "which URL does this
credential belong to" is this module's identity rule; ``mcp_oauth`` imports them
so both layers agree on what "the same server" means.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


def default_path() -> Path:
    """The credential file. Resolved per call so a redirected home (tests,
    packaged app) is honored."""
    return Path.home() / ".cluxmate" / "mcp-auth.json"


def normalize_url(url: str) -> str:
    """Scheme + host lowercased, trailing slash dropped. Everything else kept."""
    u = (url or "").strip().rstrip("/")
    if "://" not in u:
        return u
    scheme, rest = u.split("://", 1)
    host, sep, path = rest.partition("/")
    return f"{scheme.lower()}://{host.lower()}{sep}{path}"


def origin_of(url: str) -> str:
    """scheme://host[:port] — the comparison unit for every same-origin rule."""
    u = normalize_url(url)
    if "://" not in u:
        return ""
    scheme, rest = u.split("://", 1)
    return f"{scheme}://{rest.partition('/')[0]}"


def same_origin(a: str, b: str) -> bool:
    return bool(origin_of(a)) and origin_of(a) == origin_of(b)


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _opt_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


@dataclass
class OAuthRecord:
    """Everything needed to call an MCP server and to refresh its token."""

    server_url: str
    access_token: str
    token_endpoint: str = ""
    issuer: str = ""
    resource: str = ""
    client_id: str = ""
    client_secret: str | None = None
    secret_expires_at: float | None = None
    refresh_token: str | None = None
    expires_at: float | None = None
    scope: str = ""

    def is_fresh(self, now: float, skew: float) -> bool:
        """True when the access token is usable for at least ``skew`` more
        seconds. No expiry recorded ⇒ False (callers treat that as "refresh or
        re-authorize" rather than assuming an eternal token)."""
        return self.expires_at is not None and self.expires_at - skew > now

    def describe(self) -> dict[str, Any]:
        """The only shape ever handed to a UI / RPC caller. No secrets."""
        return {
            "authenticated": True,
            "expires_at": self.expires_at,
            "has_refresh": bool(self.refresh_token),
            "client_id": self.client_id,
            "scope": self.scope,
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "server_url": self.server_url,
            "resource": self.resource,
            "issuer": self.issuer,
            "token_endpoint": self.token_endpoint,
            "client": {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "secret_expires_at": self.secret_expires_at,
            },
            "tokens": {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at,
                "scope": self.scope,
            },
        }

    @classmethod
    def from_json(cls, entry: Any) -> "OAuthRecord | None":
        if not isinstance(entry, dict):
            return None
        client = entry.get("client")
        tokens = entry.get("tokens")
        if not isinstance(client, dict) or not isinstance(tokens, dict):
            return None
        server_url = entry.get("server_url")
        access = tokens.get("access_token")
        if not isinstance(server_url, str) or not server_url:
            return None
        if not isinstance(access, str) or not access:
            return None
        client_id = _str(client.get("client_id"))
        if not client_id:
            return None
        return cls(
            server_url=server_url,
            access_token=access,
            token_endpoint=_str(entry.get("token_endpoint")),
            issuer=_str(entry.get("issuer")),
            resource=_str(entry.get("resource")),
            client_id=client_id,
            client_secret=_opt_str(client.get("client_secret")),
            secret_expires_at=_opt_float(client.get("secret_expires_at")),
            refresh_token=_opt_str(tokens.get("refresh_token")),
            expires_at=_opt_float(tokens.get("expires_at")),
            scope=_str(tokens.get("scope")),
        )


class MCPAuthStore:
    """Read/write access to the credential file. One instance per user.

    FRESHNESS: the file is written by OTHER instances — the JSON-RPC auth thread,
    a `cluxmate mcp auth` run in a terminal next to a live desktop session — while
    this one stays alive, so a construction-time snapshot is not enough. Every
    operation re-reads unless the file is byte-identical to the snapshot.

    Content, not mtime: the (mtime_ns, size) signature cached by
    ``core/ssrf_config.py`` is not enough for this file. Windows advances the
    file clock in ~15.6 ms ticks, so two consecutive rewrites carry the same
    mtime, and a re-issued token whose JSON happens to be the same length keeps
    the size too — a long-lived reader would then keep serving a token the writer
    already replaced. An auth/logout round trip is one file read of a <1 KB
    document, next to an OAuth exchange or a browser launch, so re-reading is the
    cheap side of that trade.
    """

    def __init__(self, path: Path | None = None):
        self._path = Path(path) if path is not None else default_path()
        self._lock = threading.Lock()
        self._servers: dict[str, Any] = {}
        self._raw: str | None = None
        with self._lock:
            self._reload_locked()

    @property
    def path(self) -> Path:
        return self._path

    def _read_raw(self) -> str | None:
        """The file's text, or None when it is missing / unreadable / not UTF-8.
        This is the change signal: see the freshness note on the class."""
        try:
            return self._path.read_text("utf-8")
        except (OSError, UnicodeDecodeError, ValueError):
            return None

    def _reload_locked(self) -> None:
        """(Re)read the file. Keeps only entries that parse into an OAuthRecord:
        a hand-corrupted entry must not survive a later rewrite of the file."""
        self._raw = self._read_raw()
        self._servers = {}
        if self._raw is None:
            return
        try:
            data = json.loads(self._raw)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        servers = data.get("servers")
        if not isinstance(servers, dict):
            return
        for name, entry in servers.items():
            if isinstance(name, str) and OAuthRecord.from_json(entry) is not None:
                self._servers[name] = entry

    def _refresh_locked(self) -> None:
        """Re-read if the file changed underneath us. Caller holds the lock."""
        if self._read_raw() != self._raw:
            self._reload_locked()

    def _save_locked(self) -> None:
        tmp = self._path.with_name(
            f".{self._path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        )
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(
                    {"version": SCHEMA_VERSION, "servers": self._servers},
                    indent=2,
                    ensure_ascii=False,
                ),
                "utf-8",
            )
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass  # Windows: mode is a no-op; the home ACL is the boundary
            os.replace(tmp, self._path)
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass
            self._raw = self._read_raw()
        except OSError:
            traceback.print_exc(file=sys.stderr)
            try:
                tmp.unlink()
            except OSError:
                pass

    def get(self, name: str, url: str) -> OAuthRecord | None:
        """Credentials for ``name``, or None when absent / corrupt / issued for
        a different URL than ``url``."""
        with self._lock:
            self._refresh_locked()
            entry = self._servers.get(name)
        record = OAuthRecord.from_json(entry)
        if record is None:
            return None
        if normalize_url(record.server_url) != normalize_url(url or ""):
            return None
        return record

    def put(self, name: str, record: OAuthRecord) -> None:
        with self._lock:
            self._refresh_locked()
            self._servers[name] = record.to_json()
            self._save_locked()

    def delete(self, name: str) -> bool:
        with self._lock:
            self._refresh_locked()
            if name not in self._servers:
                return False
            del self._servers[name]
            self._save_locked()
            return True

    def describe(self, name: str, url: str) -> dict[str, Any] | None:
        record = self.get(name, url)
        return record.describe() if record is not None else None
