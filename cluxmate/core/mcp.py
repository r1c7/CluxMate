"""MCP (Model Context Protocol) client support.

Loads MCP servers from ~/.cluxmate/mcp.json + <cwd>/.cluxmate/mcp.json
(project deep-merges over global), spawns stdio subprocesses or opens HTTP
clients, lists their tools, and exposes each as a BaseTool subclass on the
parent agent's toolset (named mcp__<server>__<tool>).

Threading constraint: subprocesses are spawned with sync subprocess.Popen
at MCPManager.load() time (main thread, no event loop). Tool calls happen
inside the agent's per-turn asyncio loop, but the call itself is sync
(Popen stdin/stdout or sync httpx.Client), bridged via run_in_executor —
same pattern as BashTool. Using asyncio.create_subprocess_exec would bind
stdio pipes to the per-turn loop, which is closed at turn end (see
jsonrpc_server.py:_run) — pipes would die. Sync Popen sidesteps that.

Per-call timeout: MCPClient.call_tool enforces its own timeout (default
60s) on top of the agent loop's 180s. The per-turn ThreadPoolExecutor is
destroyed at turn end with wait=False, so a stuck run_in_executor worker
would leak and exhaust the pool. The MCP-level timeout kills the
subprocess to unblock the read.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from cluxmate.core.mcp_auth_store import MCPAuthStore
from cluxmate.core.mcp_oauth import (
    EXPIRY_SKEW_S,
    Challenge,
    OAuthError,
    OAuthFlowConfig,
    parse_www_authenticate,
    refresh_access_token,
)
from cluxmate.tools.base import BaseTool

# Server and tool names must be identifier-safe so the mcp__<server>__<tool>
# composite name stays parseable and doesn't collide with native tools.
_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# ${VAR} references in mcp.json string fields (command/args/url/headers/env)
# are expanded from the process environment at load time so secrets (DB DSNs,
# API tokens) live in env vars instead of the file. Unknown vars expand to ""
# (envsubst/shell semantics) — a missing secret surfaces as a connection error
# rather than a literal ${...} the server can't parse.
_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Per-server handshake timeout at load. A slow server shouldn't block session
# creation — fail-soft marks it "failed" and the others proceed.
_HANDSHAKE_TIMEOUT_S = 5.0

# Default per-call tool execution timeout. The agent loop's 180s timeout is
# a backup, not the primary guard — see module docstring.
_DEFAULT_CALL_TIMEOUT_S = 60.0

# MCP protocol version we advertise in the initialize handshake.
_PROTOCOL_VERSION = "2024-11-05"


@dataclass
class MCPConfig:
    """A configured MCP server. Transport is derived: command→stdio, url→http."""
    name: str
    transport: str  # 'stdio' | 'http'
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    authorization_env: str | None = None
    disabled: bool = False
    risk_level: str = "write"  # 'safe' | 'write' | 'dangerous'
    call_timeout_s: float = _DEFAULT_CALL_TIMEOUT_S
    # OAuth: None = off for this server. An OAuthFlowConfig with no client_id
    # means "latent" — a stored token is attached if one exists, and a 401 turns
    # into needs_auth instead of a hard failure.
    oauth: OAuthFlowConfig | None = None
    oauth_error: str | None = None


def _validate_name(name: str) -> bool:
    return bool(_NAME_RE.match(name))


def _expand_env(value: Any) -> Any:
    """Recursively expand ${VAR} refs in strings / lists / dicts from os.environ.

    Only string leaves are touched; other scalars pass through. Unknown vars
    expand to "" (shell/envsubst semantics). Keys in dicts are left untouched —
    only values are expanded, so server names / header names stay literal.
    """
    if isinstance(value, str):
        return _ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


def _has_static_auth(headers: dict[str, str]) -> bool:
    """True when a non-empty Authorization header is configured. Header names are
    case-insensitive (RFC 9110), so a lowercase `authorization` must suppress
    OAuth just like `Authorization` does. A non-string value (a hand-edited
    `mcp.json` can hold a number) is not a usable credential — it neither
    suppresses OAuth nor raises."""
    return any(
        k.lower() == "authorization" and isinstance(v, str) and bool(v.strip())
        for k, v in headers.items()
    )


def _derive_transport(entry: dict[str, Any]) -> str:
    if "command" in entry and entry["command"]:
        return "stdio"
    if "url" in entry and entry["url"]:
        return "http"
    raise ValueError("MCP server entry must have 'command' (stdio) or 'url' (http)")


class MCPConfigManager:
    """Load and merge mcp.json from global and project roots.

    Global: ~/.cluxmate/mcp.json
    Project: <cwd>/.cluxmate/mcp.json (deep-merges over global; project can
    override per-server fields or add new servers).
    """

    def __init__(self, cwd: str):
        self._cwd = str(Path(cwd).resolve()) if cwd else str(Path.cwd())

    def _roots(self) -> list[Path]:
        return [
            Path.home() / ".cluxmate" / "mcp.json",
            Path(self._cwd) / ".cluxmate" / "mcp.json",
        ]

    def load(self) -> dict[str, MCPConfig]:
        merged: dict[str, dict[str, Any]] = {}
        for path in self._roots():
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text("utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            servers = data.get("mcpServers", {})
            if not isinstance(servers, dict):
                continue
            for name, entry in servers.items():
                if not _validate_name(name) or not isinstance(entry, dict):
                    continue
                if name in merged:
                    merged[name].update(entry)
                else:
                    merged[name] = dict(entry)

        configs: dict[str, MCPConfig] = {}
        for name, entry in merged.items():
            try:
                transport = _derive_transport(entry)
            except ValueError:
                continue
            # Expand ${VAR} refs from the environment in every value-bearing
            # field so secrets (DB DSNs, tokens) can live in env vars, not the
            # file. authorization_env is intentionally NOT expanded — it holds
            # an env var *name*, not a value.
            headers = _expand_env(dict(entry.get("headers", {})))
            auth_env = entry.get("authorization_env") or entry.get("authorizationEnv")
            if auth_env:
                token = os.environ.get(auth_env, "")
                if token:
                    headers.setdefault("Authorization", f"Bearer {token}")
            risk = entry.get("risk_level", "write")
            if risk not in ("safe", "write", "dangerous"):
                risk = "write"
            url = _expand_env(entry.get("url"))
            # OAuth precedence (see the design doc §4.2): an explicit oauth
            # object wins over a static Authorization header, which in turn wins
            # over latent OAuth. A server with no credentials at all still gets
            # latent OAuth so a 401 reads as "log in" rather than "broken".
            raw_oauth = entry.get("oauth")
            explicit_oauth = raw_oauth if isinstance(raw_oauth, dict) else None
            # A truthy non-object (`"oauth": true`) means "enable with defaults":
            # silently ignoring it would turn a user's intent into a confusing 401,
            # and on a stdio server it must reach the config error below.
            enable_with_defaults = bool(raw_oauth) and explicit_oauth is None
            opts = explicit_oauth if explicit_oauth is not None else {}
            oauth_cfg: OAuthFlowConfig | None = None
            oauth_error: str | None = None
            if raw_oauth is False:
                oauth_cfg = None
            elif explicit_oauth is not None or enable_with_defaults:
                secret_env = opts.get("client_secret_env") \
                    or opts.get("clientSecretEnv")
                secret = os.environ.get(secret_env, "") if secret_env else ""
                try:
                    callback_port = int(opts.get("callback_port", 0) or 0)
                except (TypeError, ValueError):
                    callback_port = 0
                oauth_cfg = OAuthFlowConfig(
                    server_name=name,
                    server_url=url or "",
                    client_id=_expand_env(opts.get("client_id")) or None,
                    client_secret=secret or None,
                    scopes=_expand_env(opts.get("scopes")) or None,
                    callback_port=callback_port,
                )
            elif transport == "http" and not _has_static_auth(headers):
                oauth_cfg = OAuthFlowConfig(server_name=name, server_url=url or "")
            if oauth_cfg is not None and transport != "http":
                oauth_error = "oauth is only supported for remote (url) MCP servers"
                oauth_cfg = None
            configs[name] = MCPConfig(
                name=name,
                transport=transport,
                command=_expand_env(entry.get("command")),
                args=_expand_env(list(entry.get("args", []))),
                env=_expand_env(dict(entry.get("env", {}))),
                url=url,
                headers=headers,
                authorization_env=auth_env,
                disabled=bool(entry.get("disabled", False)),
                risk_level=risk,
                call_timeout_s=float(entry.get("call_timeout_s", _DEFAULT_CALL_TIMEOUT_S)),
                oauth=oauth_cfg,
                oauth_error=oauth_error,
            )
        return configs


class MCPClient:
    """One MCP server connection.

    Sync throughout (Popen stdin/stdout or httpx.Client). Called via
    run_in_executor from the agent loop — see module docstring.
    """

    def __init__(self, config: MCPConfig, sandbox=None, cwd: str | None = None,
                 egress_mode: str = "shared", auth_store: MCPAuthStore | None = None):
        self.config = config
        self._sandbox = sandbox  # ShellSandbox | None (stdio servers only)
        self._cwd = cwd or os.getcwd()
        self._egress_mode = egress_mode
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._http: httpx.Client | None = None
        self._next_id = 0
        self._tools: list[dict[str, Any]] = []
        # 'disconnected' | 'connected' | 'failed' | 'needs_auth' | 'disabled'
        self._status: str = "disconnected"
        self._error: str | None = None
        # OAuth state. `_auth_store` is only consulted when the config carries a
        # usable OAuthFlowConfig — a stdio server or a statically-authenticated
        # one never touches the credential file.
        self._auth_store = auth_store if config.oauth is not None else None
        self._auth_challenge: Challenge | None = None
        self._auth_error: str | None = None

    def _next_request_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def start(self) -> bool:
        """Spawn subprocess / open HTTP client + run MCP initialize handshake.

        Returns True on success. On failure, sets _status='failed' and _error.
        """
        if self.config.oauth_error:
            # Misconfigured (e.g. oauth on a stdio server): fail fast and
            # visibly before spawning anything.
            self._status = "failed"
            self._error = self.config.oauth_error
            return False
        try:
            if self.config.transport == "stdio":
                # Resolve the executable through PATH (+ PATHEXT on Windows).
                # subprocess.Popen without shell=True does NOT apply PATHEXT, so
                # a bare "npx"/"docker" from a standard mcp.json fails with
                # WinError 2 even though the .cmd/.exe is on PATH. shutil.which
                # finds "npx.cmd" from "npx". Fall back to the raw command if
                # which() misses (e.g. an absolute path or a shell builtin).
                resolved = shutil.which(self.config.command) or self.config.command
                cmd = [resolved] + self.config.args
                env = os.environ.copy()
                env.update(self.config.env)
                env["PYTHONUTF8"] = "1"
                env["PYTHONIOENCODING"] = "utf-8"
                # stdin=PIPE so we can write JSON-RPC requests. stdout=PIPE for
                # responses. stderr=DEVNULL keeps our stdout clean. bufsize=1
                # is line-buffered so each json.dumps()+"\n" flushes promptly.
                if self._sandbox is not None:
                    # Best-effort shell sandbox for stdio servers: a user
                    # configures the server (not the model), so no backend →
                    # fall back to a bare Popen rather than fail-closed (that
                    # severity is reserved for model-generated bash). When a
                    # backend IS present, the server runs low-IL/bwrap —
                    # supply-chain-compromised or injected servers can no
                    # longer write outside the workspace.
                    self._proc = self._sandbox.spawn_popen(
                        cmd, cwd=self._cwd, env=env,
                    )
                else:
                    self._proc = subprocess.Popen(
                        cmd,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        bufsize=1,
                        env=env,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    )
            else:
                self._http = httpx.Client(
                    base_url=self.config.url,
                    headers=self.config.headers,
                    timeout=self.config.call_timeout_s,
                )

            # MCP initialize handshake — required by the spec before any
            # tools/list or tools/call. Server responds with its capabilities.
            init_resp = self._send_request("initialize", {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "cluxmate", "version": "1.0"},
            })
            if init_resp is None or "error" in init_resp:
                # A 401 during the handshake already recorded `needs_auth` (and
                # the actionable "run: cluxmate mcp auth …" message) in
                # _send_http — do not downgrade that to a generic failure.
                if self._status != "needs_auth":
                    self._status = "failed"
                    self._error = self._error or "initialize handshake failed"
                self._cleanup()
                return False
            # initialized notification — no response expected.
            self._send_notification("notifications/initialized", {})
            self._status = "connected"
            return True
        except Exception as e:
            self._status = "failed"
            self._error = str(e)
            self._cleanup()
            return False

    def _send_request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Send a JSON-RPC request, wait for the matching response. None on fail."""
        req_id = self._next_request_id()
        req = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        try:
            with self._lock:
                if self.config.transport == "stdio":
                    return self._send_stdio(req, req_id)
                return self._send_http(req, req_id)
        except Exception as e:
            self._error = str(e)
            return None

    def _send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        notif = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        try:
            with self._lock:
                if self.config.transport == "stdio":
                    if self._proc and self._proc.stdin:
                        self._proc.stdin.write(json.dumps(notif, ensure_ascii=False) + "\n")
                        self._proc.stdin.flush()
                elif self._http:
                    # Same two fixes as _send_http: httpx ≥ 0.28 forces a
                    # trailing slash onto base_url, so a relative post("")
                    # lands on "<url>/" (404) instead of the MCP endpoint; and
                    # the bearer goes on THIS request only — never merged into
                    # the client's static headers (which `conflict` reads).
                    # A 401-gated server rejects a credential-less
                    # notifications/initialized, so it must carry the bearer.
                    headers: dict[str, str] = {}
                    bearer, may_send = self._ensure_bearer()
                    if may_send and bearer:
                        headers["Authorization"] = f"Bearer {bearer}"
                    try:
                        self._http.post(self.config.url, json=notif, headers=headers)
                    except Exception:
                        pass  # notifications have no response; ignore
        except Exception:
            pass

    def _send_stdio(self, req: dict[str, Any], req_id: int) -> dict[str, Any] | None:
        assert self._proc is not None and self._proc.stdin and self._proc.stdout
        line = json.dumps(req, ensure_ascii=False) + "\n"
        self._proc.stdin.write(line)
        self._proc.stdin.flush()
        # Watchdog timer: if the server doesn't respond within the call
        # timeout, kill the subprocess to unblock the stdout readline.
        # Otherwise we'd leak the worker (see module docstring).
        watchdog = threading.Timer(self.config.call_timeout_s, self._kill_proc)
        watchdog.start()
        try:
            # readline() loop (not `for raw in self._proc.stdout`) — the
            # iterator over a pipe does read-ahead buffering and can stall
            # on small payloads. readline() yields each line as it arrives.
            while True:
                raw = self._proc.stdout.readline()
                if not raw:
                    # EOF — subprocess died
                    self._status = "failed"
                    self._error = "subprocess exited"
                    return None
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                # Skip notifications (no id) — only match our request id.
                if msg.get("id") == req_id:
                    return msg
        finally:
            watchdog.cancel()

    def _kill_proc(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.kill()
            except Exception:
                pass

    def _send_http(self, req: dict[str, Any], req_id: int) -> dict[str, Any] | None:
        assert self._http is not None
        # The bearer goes on THIS request only. self._http's static headers are
        # the configured ones (and the `conflict` check reads them), so a token
        # must never be merged into the client and leak to another origin on a
        # redirect or a later rebuild.
        headers: dict[str, str] = {}
        bearer, may_send = self._ensure_bearer()
        if not may_send:
            return None
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        # Absolute URL, not "": httpx ≥ 0.28 forces a trailing slash onto
        # base_url, so a relative POST to "" lands on "<url>/" and every real
        # server answers 404 instead of the JSON-RPC response.
        resp = self._http.post(self.config.url or "", json=req, headers=headers)
        if resp.status_code in (401, 403) and self._auth_store is not None:
            self._auth_challenge = parse_www_authenticate(
                resp.headers.get("www-authenticate", "")
            )
            scope = self._auth_challenge.scope if self._auth_challenge else None
            detail = "rejected the token" if resp.status_code == 403 else "requires authorization"
            if resp.status_code == 403 and scope:
                detail = f"requires additional scope ({scope})"
            self._auth_failure(
                f"server '{self.config.name}' {detail} — run: "
                f"cluxmate mcp auth {self.config.name}",
                needs_auth=True,
            )
            return None
        resp.raise_for_status()
        msg = resp.json()
        if msg.get("id") != req_id:
            # Response id mismatch — protocol violation. Treat as failure.
            self._error = f"id mismatch: expected {req_id}, got {msg.get('id')}"
            return None
        return msg

    def challenge(self) -> Challenge | None:
        """The last 401 challenge (resource_metadata / scope), if any. Reused by
        the authorization flow so it skips a redundant probe request."""
        return self._auth_challenge

    def _auth_failure(self, message: str, *, needs_auth: bool) -> None:
        self._status = "needs_auth" if needs_auth else "failed"
        self._error = message

    def _ensure_bearer(self) -> tuple[str | None, bool]:
        """(bearer_to_attach, may_send). may_send=False means a reason has
        already been recorded and no credential-less request may go on the wire.

        Silent by construction: this never opens a browser. A missing or
        URL-mismatched record returns (None, True) so the server's 401 becomes
        the trigger for the user-visible flow.
        """
        cfg = self.config.oauth
        if self._auth_store is None or cfg is None:
            return None, True
        record = self._auth_store.get(self.config.name, self.config.url or "")
        if record is None:
            return None, True
        if record.is_fresh(time.time(), EXPIRY_SKEW_S):
            return record.access_token, True
        if not record.refresh_token:
            self._auth_store.delete(self.config.name)
            self._auth_failure(
                f"server '{self.config.name}' authorization expired and no refresh "
                f"token is stored — run: cluxmate mcp auth {self.config.name}",
                needs_auth=True,
            )
            return None, False
        try:
            # No explicit auth_method: the record carries how the AS wants the
            # client authenticated (client_secret_basic vs _post vs none).
            refreshed = refresh_access_token(
                record, token_endpoint=record.token_endpoint,
                http_timeout=self.config.call_timeout_s,
            )
        except OAuthError as e:
            if e.kind == "refresh_rejected":
                self._auth_store.delete(self.config.name)
                self._auth_failure(
                    f"server '{self.config.name}' rejected the stored OAuth token "
                    f"({e}) — run: cluxmate mcp auth {self.config.name}",
                    needs_auth=True,
                )
            else:
                # Transient (timeout / 5xx): keep the credentials so the next
                # call can retry, and do NOT claim the user must log in again.
                self._auth_failure(
                    f"could not refresh the OAuth token for '{self.config.name}': {e}",
                    needs_auth=False,
                )
            return None, False
        self._auth_store.put(self.config.name, refreshed)
        return refreshed.access_token, True

    def _force_refresh(self) -> bool:
        """Refresh once, ignoring the expiry window. Returns True when a new
        access token is stored and the request may be retried."""
        if self._auth_store is None:
            return False
        record = self._auth_store.get(self.config.name, self.config.url or "")
        if record is None or not record.refresh_token:
            return False
        try:
            refreshed = refresh_access_token(
                record, token_endpoint=record.token_endpoint,
                http_timeout=self.config.call_timeout_s,
            )
        except OAuthError as e:
            if e.kind == "refresh_rejected":
                self._auth_store.delete(self.config.name)
            self._auth_failure(str(e), needs_auth=e.kind == "refresh_rejected")
            return False
        self._auth_store.put(self.config.name, refreshed)
        self._status = "connected"
        self._error = None
        return True

    def _send_request_authed(self, method: str, params: dict[str, Any] | None = None):
        """One forced refresh + one retry per call — never a refresh loop."""
        resp = self._send_request(method, params)
        if resp is None and self._status == "needs_auth":
            if self._force_refresh():
                resp = self._send_request(method, params)
        return resp

    def list_tools(self) -> list[dict[str, Any]]:
        """Fetch tools/list. Called once at handshake. Updates _status on failure."""
        resp = self._send_request_authed("tools/list")
        if resp is None:
            # _send_request already set _error; mark failed if it was a transport error.
            if self._status == "connected":
                self._status = "failed"
            return []
        if "error" in resp:
            self._error = f"tools/list error: {resp['error']}"
            return []
        tools = resp.get("result", {}).get("tools", [])
        self._tools = tools if isinstance(tools, list) else []
        return self._tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Invoke a tool. Returns text content from the result."""
        resp = self._send_request_authed(
            "tools/call", {"name": name, "arguments": arguments}
        )
        if resp is None:
            if self._status == "connected":
                self._status = "failed"
            return f"Error: MCP call failed: {self._error or 'no response'}"
        if "error" in resp:
            return f"Error: {resp['error']}"
        result = resp.get("result", {})
        content = result.get("content", [])
        if not isinstance(content, list):
            return "(no output)"
        texts = [
            c.get("text", "") for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        ]
        return "\n".join(t for t in texts if t) or "(no output)"

    def status(self) -> dict[str, Any]:
        """Live status: stdio uses proc.poll() (cheap, no I/O); http is last-known."""
        if self.config.disabled:
            status = "disabled"
        elif self.config.transport == "stdio" and self._proc is not None:
            if self._proc.poll() is not None:
                status = "failed"
            else:
                status = self._status
        else:
            status = self._status
        # Translate the spec transport name to the user-facing label the
        # desktop UI expects ('stdio' → 'local', 'http' → 'remote'). The
        # internal MCPConfig.transport keeps the spec name for use in
        # start()/shutdown() branching; only the JSON-RPC output is mapped.
        transport_label = "local" if self.config.transport == "stdio" else "remote"
        egress = self._egress_mode
        if egress == "off" and platform.system() == "Windows":
            egress = "off (ineffective on Windows)"
        # OAuth state for the UI. Built from the record's non-secret fields only
        # — the access/refresh token values never reach this dict.
        oauth: dict[str, Any] | None = None
        if self.config.oauth is not None:
            record = (
                self._auth_store.get(self.config.name, self.config.url or "")
                if self._auth_store is not None else None
            )
            oauth = {
                "enabled": True,
                "authenticated": record is not None,
                "expires_at": record.expires_at if record else None,
                "has_refresh": bool(record and record.refresh_token),
                # Same rule the loader uses to decide whether a static header
                # suppresses OAuth: case-insensitive and non-string safe, so a
                # lowercase `authorization` is reported as the shadowing
                # credential it truly is (see _has_static_auth).
                "conflict": "static_header" if _has_static_auth(self.config.headers) else None,
            }
        return {
            "name": self.config.name,
            "transport": transport_label,
            "status": status,
            "disabled": self.config.disabled,
            "egress": egress,
            "error": self._error,
            "oauth": oauth,
            "tools": [
                {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "input_schema": t.get("inputSchema", {}) or {},
                }
                for t in self._tools
            ],
        }

    def shutdown(self) -> None:
        """Kill subprocess / close HTTP client. Idempotent.

        Kills the process BEFORE acquiring the lock. If a worker thread is
        in _send_stdio holding the lock and waiting on readline, killing the
        process unblocks it with EOF — _send_request releases the lock — we
        can then lock and clean up without deadlocking for call_timeout_s.
        """
        # Step 1: kill without the lock (unblocks any readline waiters)
        if self._proc is not None:
            try:
                if self._proc.poll() is None:
                    self._proc.kill()
            except Exception:
                pass
        if self._http is not None:
            try:
                self._http.close()
            except Exception:
                pass
        # Step 2: lock and null the references
        with self._lock:
            self._proc = None
            self._http = None
            self._status = "disconnected"

    def _cleanup(self) -> None:
        """Release proc/http references. Only called from start() on a
        freshly-spawned client — nobody else can be in _send_request, so
        the lock is uncontested.
        """
        if self._proc is not None:
            try:
                if self._proc.stdin:
                    try:
                        self._proc.stdin.close()
                    except Exception:
                        pass
                if self._proc.poll() is None:
                    try:
                        self._proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self._proc.kill()
            except Exception:
                pass
            finally:
                self._proc = None
        if self._http is not None:
            try:
                self._http.close()
            except Exception:
                pass
            finally:
                self._http = None


class MCPManager:
    """Manages all configured MCP clients for a working directory.

    Constructed once per parent AgentBuilder and cached (NOT per-turn —
    spawning subprocesses every turn would be broken). load() spawns
    subprocesses and runs the tools/list handshake in parallel with a
    per-server timeout. list_tools() returns wrapped tools for the agent.
    shutdown() kills everything — called from atexit and the mcp/shutdown
    JSON-RPC method.
    """

    def __init__(self, cwd: str, sandbox=None, egress_mode: str = "shared",
                 auth_store: MCPAuthStore | None = None):
        self._cwd = cwd
        self._sandbox = sandbox  # ShellSandbox | None — passed to stdio clients
        self._egress_mode = egress_mode
        # One store per manager, shared by every client it builds: the file is
        # re-read on each access, so an out-of-band `cluxmate mcp auth` run (or
        # the JSON-RPC auth flow) is visible to the live clients without a
        # rebuild.
        self._auth_store = auth_store if auth_store is not None else MCPAuthStore()
        self._configs: dict[str, MCPConfig] = {}
        self._clients: dict[str, MCPClient] = {}
        self._tools: list[MCPToolWrapper] = []
        self._loaded = False
        # Latches on shutdown(): reload_client() must never respawn a client
        # into a manager that has already been torn down.
        self._closed = False
        # Register shutdown at process exit. atexit fires on normal interpreter
        # exit (stdin EOF, SIGTERM on Linux/Mac). Windows TerminateProcess
        # skips atexit — the mcp/shutdown RPC is the fallback for that case
        # (sent by agent-bridge.kill() before proc.kill()).
        atexit.register(self.shutdown)

    def load(self) -> None:
        """Read config, spawn clients, run tools/list handshake. Idempotent."""
        if self._loaded:
            return
        self._loaded = True
        self._configs = MCPConfigManager(self._cwd).load()

        for cfg in self._configs.values():
            self._clients[cfg.name] = MCPClient(
                cfg, sandbox=self._sandbox, cwd=self._cwd,
                egress_mode=self._egress_mode, auth_store=self._auth_store,
            )

        if not self._clients:
            return

        # Parallel start + handshake. Each future is bounded to slightly
        # above _HANDSHAKE_TIMEOUT_S so a slow server fails-soft without
        # blocking the others. Slow server → _status='failed', continues.
        #
        # CRITICAL: Do NOT use `with ThreadPoolExecutor` — __exit__ calls
        # shutdown(wait=True) which blocks until every submitted future
        # completes. A worker stuck on a slow npx download or unresponsive
        # server would hold load() hostage for up to 60s (the per-call
        # watchdog), which in turn delays _handle_initialize's response to
        # the desktop (the init response carries the tool list, and building
        # it calls _get_tools, which calls load()). The desktop then shows a
        # spinner until initialize resolves — which, in the worst case, is
        # 60s for every configured server.
        workers = max(1, min(8, len(self._clients)))
        ex = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = {
                name: ex.submit(self._start_and_handshake, client)
                for name, client in self._clients.items()
            }
            for name, fut in futures.items():
                try:
                    fut.result(timeout=_HANDSHAKE_TIMEOUT_S + 1.0)
                except FutureTimeout:
                    self._clients[name]._status = "failed"
                    self._clients[name]._error = "handshake timeout"
                    # Kill the subprocess so the stuck worker thread
                    # (still blocked on readline() or TCP recv()) gets
                    # EOF / connection error and exits on its own.
                    self._clients[name].shutdown()
                except Exception as e:
                    self._clients[name]._status = "failed"
                    self._clients[name]._error = str(e)
        finally:
            # Don't wait for timed-out workers — they were killed above and
            # will exit shortly (EOF on stdout pipe). A true-hang worker
            # (network I/O to an unreachable host) will exit when the OS
            # times out the TCP connection or the MCP process is killed.
            ex.shutdown(wait=False)

        self._rebuild_tools()

    def _rebuild_tools(self) -> None:
        """Rebuild the exposed tool list from every CONNECTED client. Called by
        load() and by reload_client() — one place, so a reload can never leave
        stale wrappers pointing at a shut-down client."""
        tools: list[MCPToolWrapper] = []
        for client in self._clients.values():
            if client.config.disabled or client._status != "connected":
                continue
            for tool in client._tools:
                tool_name = tool.get("name", "")
                if not tool_name or not _validate_name(tool_name):
                    continue
                tools.append(MCPToolWrapper(
                    client=client,
                    tool_name=tool_name,
                    description=tool.get("description", ""),
                    input_schema=tool.get("inputSchema", {}) or {"type": "object", "properties": {}},
                    risk_level=client.config.risk_level,
                    cwd=self._cwd,
                ))
        self._tools = tools

    def config(self, name: str) -> MCPConfig | None:
        return self._configs.get(name)

    def client(self, name: str) -> MCPClient | None:
        return self._clients.get(name)

    def reload_client(self, name: str) -> bool:
        """Re-spawn ONE server (its credentials or config changed) and refresh
        the tool list. Returns True when the exposed wrappers were replaced (a
        successful reconnect ALWAYS counts, even when the tool names are
        identical).

        A failed start is NOT an error here: the new client keeps whatever status
        start() recorded (needs_auth / failed) and the old wrappers are dropped —
        a stale tool set is worse than none, because every call on it would fail.
        """
        if not self._loaded or self._closed:
            return False
        cfg = self._configs.get(name)
        if cfg is None:
            return False
        old = self._clients.get(name)
        if old is not None:
            try:
                old.shutdown()
            except Exception:
                pass
        before = {(t._client.config.name, t._tool_name) for t in self._tools}
        client = MCPClient(
            cfg, sandbox=self._sandbox, cwd=self._cwd,
            egress_mode=self._egress_mode, auth_store=self._auth_store,
        )
        self._clients[name] = client
        self._start_and_handshake(client)
        if self._closed:
            # A concurrent shutdown() tore the manager down while we were
            # handshaking: it cleared _clients before this client existed, so it
            # cannot reach it — without this the transport (or, for stdio, the
            # subprocess) would outlive the manager entirely.
            try:
                client.shutdown()
            except Exception:
                pass
            self._clients.pop(name, None)
            return False
        self._rebuild_tools()
        after = {(t._client.config.name, t._tool_name) for t in self._tools}
        # A reload REPLACES the wrappers for this server even when the exposed
        # tool names are identical, and the old wrappers now point at a client
        # that was just shut down. Reporting "unchanged" would leave the agent
        # dispatching through them (every call answering "no response") until a
        # session restart — so a successful reconnect always counts as a change.
        return client._status == "connected" or before != after

    def _start_and_handshake(self, client: MCPClient) -> None:
        if not client.start():
            return  # _status already 'failed'
        client.list_tools()

    def list_tools(self) -> list[MCPToolWrapper]:
        return list(self._tools)

    def status(self) -> list[dict[str, Any]]:
        return [client.status() for client in self._clients.values()]

    def shutdown(self) -> None:
        # FIRST statement: latch closed so a reload_client() racing this teardown
        # refuses / reclaims instead of leaving an untracked live transport.
        self._closed = True
        # Snapshot: a concurrent reload_client() may pop an entry (and another
        # thread may add one) while this loop runs, and iterating the live dict
        # would raise RuntimeError mid-teardown, aborting the rest of the kills.
        for client in list(self._clients.values()):
            try:
                client.shutdown()
            except Exception:
                pass
        self._clients.clear()
        self._tools = []


class MCPToolWrapper(BaseTool):
    """A BaseTool that proxies to an MCP server's tool.

    Named mcp__<server>__<tool> to avoid colliding with native tools.
    execute() is sync at the MCP level and bridges to the agent's event loop
    via run_in_executor — same pattern as BashTool.execute.
    """

    def __init__(
        self,
        client: MCPClient,
        tool_name: str,
        description: str,
        input_schema: dict[str, Any],
        risk_level: str = "write",
        cwd: str | None = None,
    ):
        self._client = client
        self._tool_name = tool_name
        self._description = description
        self._input_schema = input_schema
        self._risk_level = risk_level
        # _workdir feeds BaseTool.spill_cwd, so an oversized MCP result is
        # spilled like a native one (None → inline truncation only).
        self._workdir = cwd

    @property
    def name(self) -> str:
        return f"mcp__{self._client.config.name}__{self._tool_name}"

    @property
    def description(self) -> str:
        return self._description or f"MCP tool {self._tool_name} on {self._client.config.name}"

    @property
    def input_schema(self) -> dict[str, Any]:
        return self._input_schema

    @property
    def risk_level(self) -> str:
        return self._risk_level

    async def execute(self, **kwargs: Any) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._client.call_tool, self._tool_name, kwargs
        )
