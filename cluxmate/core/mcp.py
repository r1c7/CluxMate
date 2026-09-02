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
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

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
            configs[name] = MCPConfig(
                name=name,
                transport=transport,
                command=_expand_env(entry.get("command")),
                args=_expand_env(list(entry.get("args", []))),
                env=_expand_env(dict(entry.get("env", {}))),
                url=_expand_env(entry.get("url")),
                headers=headers,
                authorization_env=auth_env,
                disabled=bool(entry.get("disabled", False)),
                risk_level=risk,
                call_timeout_s=float(entry.get("call_timeout_s", _DEFAULT_CALL_TIMEOUT_S)),
            )
        return configs


class MCPClient:
    """One MCP server connection.

    Sync throughout (Popen stdin/stdout or httpx.Client). Called via
    run_in_executor from the agent loop — see module docstring.
    """

    def __init__(self, config: MCPConfig, sandbox=None, cwd: str | None = None,
                 egress_mode: str = "shared"):
        self.config = config
        self._sandbox = sandbox  # ShellSandbox | None (stdio servers only)
        self._cwd = cwd or os.getcwd()
        self._egress_mode = egress_mode
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._http: httpx.Client | None = None
        self._next_id = 0
        self._tools: list[dict[str, Any]] = []
        # 'disconnected' | 'connected' | 'failed' | 'disabled'
        self._status: str = "disconnected"
        self._error: str | None = None

    def _next_request_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def start(self) -> bool:
        """Spawn subprocess / open HTTP client + run MCP initialize handshake.

        Returns True on success. On failure, sets _status='failed' and _error.
        """
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
                self._status = "failed"
                self._error = "initialize handshake failed"
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
                    try:
                        self._http.post("", json=notif)
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
        resp = self._http.post("", json=req)
        resp.raise_for_status()
        msg = resp.json()
        if msg.get("id") != req_id:
            # Response id mismatch — protocol violation. Treat as failure.
            self._error = f"id mismatch: expected {req_id}, got {msg.get('id')}"
            return None
        return msg

    def list_tools(self) -> list[dict[str, Any]]:
        """Fetch tools/list. Called once at handshake. Updates _status on failure."""
        resp = self._send_request("tools/list")
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
        resp = self._send_request("tools/call", {"name": name, "arguments": arguments})
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
        return {
            "name": self.config.name,
            "transport": transport_label,
            "status": status,
            "disabled": self.config.disabled,
            "egress": egress,
            "error": self._error,
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

    def __init__(self, cwd: str, sandbox=None, egress_mode: str = "shared"):
        self._cwd = cwd
        self._sandbox = sandbox  # ShellSandbox | None — passed to stdio clients
        self._egress_mode = egress_mode
        self._configs: dict[str, MCPConfig] = {}
        self._clients: dict[str, MCPClient] = {}
        self._tools: list[MCPToolWrapper] = []
        self._loaded = False
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
                egress_mode=self._egress_mode,
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

        self._tools = []
        for client in self._clients.values():
            if client.config.disabled or client._status != "connected":
                continue
            for tool in client._tools:
                tool_name = tool.get("name", "")
                if not tool_name or not _validate_name(tool_name):
                    continue
                self._tools.append(MCPToolWrapper(
                    client=client,
                    tool_name=tool_name,
                    description=tool.get("description", ""),
                    input_schema=tool.get("inputSchema", {}) or {"type": "object", "properties": {}},
                    risk_level=client.config.risk_level,
                ))

    def _start_and_handshake(self, client: MCPClient) -> None:
        if not client.start():
            return  # _status already 'failed'
        client.list_tools()

    def list_tools(self) -> list[MCPToolWrapper]:
        return list(self._tools)

    def status(self) -> list[dict[str, Any]]:
        return [client.status() for client in self._clients.values()]

    def shutdown(self) -> None:
        for client in self._clients.values():
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
    ):
        self._client = client
        self._tool_name = tool_name
        self._description = description
        self._input_schema = input_schema
        self._risk_level = risk_level

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
