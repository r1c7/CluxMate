"""LSP (Language Server Protocol) client — multi-language code intelligence.

Follows the MCP module's lifecycle pattern: a config table + a lazy,
session-scoped manager that spawns one stdio language server per language on
first query and reuses it. Language servers are user-configured (not model
output), so they run best-effort sandboxed — never fail-closed.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname


@dataclass
class ServerSpec:
    """How to launch one language server. Command resolves on PATH (never
    bundled); install_hint surfaces when it is missing. extension_to_language
    maps file suffixes to LSP language IDs and drives file → language routing,
    so a config-only entry can add a new language without any code change."""

    command: str
    args: list[str] = field(default_factory=list)
    extension_to_language: dict[str, str] = field(default_factory=dict)
    install_hint: str = ""
    env: dict[str, str] = field(default_factory=dict)
    initialization_options: dict = field(default_factory=dict)


LSP_DEFAULT_SPECS: dict[str, ServerSpec] = {
    "python": ServerSpec(
        command="pyright-langserver",
        args=["--stdio"],
        extension_to_language={".py": "python", ".pyi": "python"},
        install_hint="npm i -g pyright",
    ),
    "typescript": ServerSpec(
        command="typescript-language-server",
        args=["--stdio"],
        extension_to_language={".ts": "typescript", ".tsx": "typescript"},
        install_hint="npm i -g typescript-language-server typescript",
    ),
    "javascript": ServerSpec(
        command="typescript-language-server",
        args=["--stdio"],
        extension_to_language={".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript"},
        install_hint="npm i -g typescript-language-server typescript",
    ),
    "go": ServerSpec(
        command="gopls",
        extension_to_language={".go": "go"},
        install_hint="go install golang.org/x/tools/gopls@latest",
    ),
    "java": ServerSpec(
        command="jdtls",
        extension_to_language={".java": "java"},
        install_hint="install eclipse.jdt.ls (jdtls): brew install jdtls / from the JDT-LS releases",
    ),
    "rust": ServerSpec(
        command="rust-analyzer",
        extension_to_language={".rs": "rust"},
        install_hint="rustup component add rust-analyzer",
    ),
    "cpp": ServerSpec(
        command="clangd",
        extension_to_language={
            ".c": "cpp", ".h": "cpp", ".cc": "cpp", ".cpp": "cpp",
            ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
        },
        install_hint="install clangd (LLVM): apt install clangd / brew install llvm / scoop install llvm",
    ),
}


def _spec_to_dict(spec: ServerSpec) -> dict[str, Any]:
    return {
        "command": spec.command,
        "args": list(spec.args),
        "extension_to_language": dict(spec.extension_to_language),
        "install_hint": spec.install_hint,
        "env": dict(spec.env),
        "initialization_options": dict(spec.initialization_options),
    }


def _dict_to_spec(data: dict[str, Any]) -> ServerSpec:
    ext = data.get("extension_to_language", {})
    if not isinstance(ext, dict):
        ext = {}
    return ServerSpec(
        command=data.get("command", "") or "",
        args=list(data.get("args", []) or []),
        extension_to_language={str(k): str(v) for k, v in ext.items()},
        install_hint=str(data.get("install_hint", "") or ""),
        env={str(k): str(v) for k, v in (data.get("env", {}) or {}).items()},
        initialization_options=data.get("initialization_options", {}) or {},
    )


class LSPConfigManager:
    """Load and merge lsp.json from global and project roots.

    Global: ~/.cluxmate/lsp.json
    Project: <cwd>/.cluxmate/lsp.json (deep-merges over global over defaults;
    project can override per-server fields, disable a default, or add a new
    language). Mirrors MCPConfigManager's two-root merge.
    """

    def __init__(self, cwd: str):
        self._cwd = str(Path(cwd).resolve()) if cwd else str(Path.cwd())

    def _roots(self) -> list[Path]:
        return [
            Path.home() / ".cluxmate" / "lsp.json",
            Path(self._cwd) / ".cluxmate" / "lsp.json",
        ]

    def load(self) -> dict[str, ServerSpec]:
        merged: dict[str, dict[str, Any]] = {
            lang: _spec_to_dict(spec) for lang, spec in LSP_DEFAULT_SPECS.items()
        }
        for path in self._roots():
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text("utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            servers = data.get("servers", {})
            if not isinstance(servers, dict):
                continue
            for lang, entry in servers.items():
                if not isinstance(entry, dict):
                    continue
                if lang in merged:
                    merged[lang].update(entry)
                else:
                    merged[lang] = dict(entry)

        specs: dict[str, ServerSpec] = {}
        for lang, entry in merged.items():
            if entry.get("disabled"):
                continue
            spec = _dict_to_spec(entry)
            if not spec.command:
                continue
            specs[lang] = spec
        return specs


def _path_to_uri(path: str) -> str:
    return Path(path).resolve().as_uri()


def _uri_to_path(uri: str) -> str:
    parsed = urlparse(uri)
    return url2pathname(unquote(parsed.path))


class LSPClient:
    """One stdio language server connection (sync, like MCPClient).

    Spawns the command with PIPE stdio, runs the LSP initialize → initialized
    handshake, and exposes request() with a bounded ContentModified (-32801)
    retry. Called via run_in_executor from the tool, mirroring BashTool/MCP.
    """

    def __init__(self, spec: ServerSpec, language_id: str, root: str, sandbox=None):
        self.spec = spec
        self.language_id = language_id
        self.root = root
        self._sandbox = sandbox
        self.pos_encoding = "utf-16"
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._next_id = 0
        self._docs: dict[str, tuple[int, int, float]] = {}  # uri -> (version, size, mtime)

    def _next_request_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def start(self) -> bool:
        """Spawn + initialize handshake. Returns True on success."""
        resolved = shutil.which(self.spec.command)
        if resolved is None:
            raise RuntimeError(
                f"language server \"{self.spec.command}\" not found on PATH. "
                f"Install it: {self.spec.install_hint}"
            )
        cmd = [resolved] + self.spec.args
        env = os.environ.copy()
        env.update(self.spec.env)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        if self._sandbox is not None:
            self._proc = self._sandbox.spawn_popen(cmd, cwd=self.root, env=env)
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
        result = self.request("initialize", {
            "processId": os.getpid(),
            "rootUri": _path_to_uri(self.root),
            "capabilities": {
                "general": {"positionEncodings": ["utf-8", "utf-16"]},
                "textDocument": {"publishDiagnostics": {"versionSupport": True}},
            },
            "initializationOptions": self.spec.initialization_options,
        }, retries=0)
        caps = (result or {}).get("capabilities", {}) or {}
        enc = caps.get("positionEncoding")
        if enc in ("utf-8", "utf-16"):
            self.pos_encoding = enc
        self._notify("initialized", {})
        return True

    def _write(self, payload: dict) -> None:
        """Write one message using LSP's Content-Length framing.

        LSP stdio is NOT newline-delimited JSON: every message is prefixed
        with a ``Content-Length: <byte-count>\\r\\n\\r\\n`` header and the body
        is exactly that many UTF-8 bytes, with no trailing newline. We write
        to the raw binary buffer so the byte count is exact regardless of
        non-ASCII content.
        """
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        frame = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        stream = self._proc.stdin.buffer
        stream.write(frame)
        stream.flush()

    def _read(self) -> dict | None:
        """Read one Content-Length framed message; None if the server exited."""
        stream = self._proc.stdout.buffer
        content_length: int | None = None
        while True:
            raw = stream.readline()
            if not raw:
                return None
            line = raw.rstrip(b"\r\n")
            if line == b"":
                break
            if line.lower().startswith(b"content-length:"):
                try:
                    content_length = int(line.split(b":", 1)[1].strip())
                except ValueError:
                    content_length = None
        if content_length is None:
            return None
        body = stream.read(content_length)
        if not body:
            return None
        try:
            return json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return None

    def _notify(self, method: str, params: dict) -> None:
        notif = {"jsonrpc": "2.0", "method": method, "params": params}
        try:
            with self._lock:
                if self._proc and self._proc.stdin:
                    self._write(notif)
        except Exception:
            pass

    def request(self, method: str, params: dict, retries: int = 5) -> Any:
        """Send a request, await the matching response, return its `result`.

        ContentModified (-32801) means the server is mid-reindex: retry a few
        times with a short backoff before surfacing it (matches Reasonix).
        """
        delay = 0.4
        for attempt in range(retries + 1):
            msg = self._send(method, params)
            if msg is None:
                raise RuntimeError(f"{method}: language server exited")
            if "error" in msg:
                code = msg["error"].get("code")
                if code == -32801 and attempt < retries:
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"{method}: {msg['error'].get('message', msg['error'])}")
            return msg.get("result")
        raise RuntimeError(f"{method}: language server still indexing")

    def _send(self, method: str, params: dict) -> dict | None:
        req_id = self._next_request_id()
        req = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        assert self._proc is not None and self._proc.stdin and self._proc.stdout
        with self._lock:
            self._write(req)
            while True:
                msg = self._read()
                if msg is None:
                    return None
                # A message carrying BOTH id and method is a server→client
                # request (workspace/configuration, client/registerCapability,
                # window/workDoneProgress/create, ...). Real servers block
                # until it is answered, so we must reply — dropping it deadlocks
                # both sides. Its id lives in the SERVER's id space, which can
                # collide with our req_id, so this check must come before the
                # response match below.
                if msg.get("method") is not None:
                    if msg.get("id") is not None:
                        self._respond_to_server_request(msg)
                    # else: a notification ($/progress, publishDiagnostics, ...)
                    # — nothing to answer, keep reading.
                    continue
                if msg.get("id") == req_id:
                    return msg

    def _respond_to_server_request(self, msg: dict) -> None:
        """Answer a server-initiated request so the server can proceed.

        We advertise no dynamic capabilities, so a minimal reply is correct:
        workspace/configuration wants one settings object per requested item
        (null = "use defaults"); everything else (registerCapability,
        workDoneProgress/create, ...) is acknowledged with a null result.
        Written directly (not via _notify) because we already hold _lock.
        """
        method = msg.get("method", "")
        if method == "workspace/configuration":
            items = (msg.get("params") or {}).get("items") or []
            result: Any = [None] * len(items)
        else:
            result = None
        try:
            self._write({"jsonrpc": "2.0", "id": msg.get("id"), "result": result})
        except Exception:
            pass

    def ensure_synced(self, uri: str, path: str) -> None:
        """didOpen (first) / didChange (content changed) using stat(size, mtime)."""
        st = os.stat(path)
        doc = self._docs.get(uri)
        if doc is not None and doc[1] == st.st_size and doc[2] == st.st_mtime:
            return
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        if doc is None:
            self._notify("textDocument/didOpen", {
                "textDocument": {"uri": uri, "languageId": self.language_id, "version": 1, "text": text},
            })
            self._docs[uri] = (1, st.st_size, st.st_mtime)
        else:
            version = doc[0] + 1
            self._notify("textDocument/didChange", {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": text}],
            })
            self._docs[uri] = (version, st.st_size, st.st_mtime)

    def shutdown(self) -> None:
        """Kill subprocess. Idempotent; unblocks readline waiters by killing first."""
        if self._proc is not None:
            try:
                if self._proc.poll() is None:
                    self._proc.kill()
            except Exception:
                pass
        with self._lock:
            self._proc = None


_MAX_LOCATIONS = 100
_MAX_RESULT_CHARS = 16_000


def _locate_symbol(line_text: str, symbol: str, encoding: str) -> int:
    """Return the symbol's column (0-based) in `line_text` under the given
    LSP position encoding ('utf-16' or 'utf-8')."""
    idx = line_text.find(symbol)
    if idx < 0:
        raise ValueError(f"symbol {symbol!r} not found on line")
    prefix = line_text[:idx]
    if encoding == "utf-8":
        return len(prefix.encode("utf-8"))
    return len(prefix.encode("utf-16-le")) // 2


def _format_locations(kind: str, locations: list[dict], root: str) -> str:
    if not locations:
        return f"no {kind} found"
    out: list[str] = []
    for loc in locations:
        uri = loc.get("uri", "")
        path = _uri_to_path(uri) if uri.startswith("file://") else uri
        rel = path
        try:
            r = os.path.relpath(path, root)
            if not r.startswith(".."):
                rel = r
        except ValueError:
            rel = path
        line = loc.get("range", {}).get("start", {}).get("line", 0) + 1
        snippet = ""
        try:
            lines = Path(path).read_text(encoding="utf-8", errors="replace").split("\n")
            if 0 <= (line - 1) < len(lines):
                snippet = lines[line - 1].strip()[:200]
        except OSError:
            pass
        out.append(f"{rel}:{line}" + (f"  {snippet}" if snippet else ""))
    rendered = "\n".join(out[:_MAX_LOCATIONS])
    if len(out) > _MAX_LOCATIONS:
        rendered += f"\n\n[truncated: {len(out)} locations, showing first {_MAX_LOCATIONS}]"
    return rendered[:_MAX_RESULT_CHARS]


def _format_hover(raw: Any) -> str:
    if not raw:
        return "no hover information"
    contents = raw.get("contents") if isinstance(raw, dict) else raw
    if isinstance(contents, dict):
        value = contents.get("value", "")
    elif isinstance(contents, list):
        value = "\n".join(
            c.get("value", "") for c in contents if isinstance(c, dict)
        )
    else:
        value = str(contents)
    if not value:
        return "no hover information"
    return value[:_MAX_RESULT_CHARS]


class LSPManager:
    """Owns lazily-spawned language servers for a session.

    Servers start on first query for their language and are reused; the
    session-scoped lifecycle (shutdown/atexit) bounds them, not a single turn.
    Concurrent first-use calls share one spawn via a starting gate.
    """

    def __init__(self, workspace_root: str, specs: dict[str, ServerSpec] | None = None, sandbox=None):
        self.ws_root = workspace_root
        self.specs = specs if specs is not None else LSPConfigManager(workspace_root).load()
        self._sandbox = sandbox
        self._ext_index: dict[str, str] = {}
        for lang, spec in self.specs.items():
            for ext in spec.extension_to_language:
                self._ext_index[ext.lower()] = lang
        self._clients: dict[str, LSPClient] = {}
        self._starting: dict[str, threading.Event] = {}
        atexit.register(self.shutdown)

    def _abs(self, path: str) -> str:
        p = Path(path)
        if not p.is_absolute():
            p = Path(self.ws_root) / p
        return str(p)

    def resolve(self, path: str) -> LSPClient:
        ext = os.path.splitext(path)[1].lower()
        lang = self._ext_index.get(ext)
        if lang is None:
            raise ValueError(f"no language server configured for {ext or path}")
        spec = self.specs.get(lang)
        if spec is None or not spec.command:
            raise ValueError(f"no language server configured for {ext} files")

        if self._clients.get(lang) is not None:
            return self._clients[lang]

        ev = self._starting.get(lang)
        if ev is None:
            ev = threading.Event()
            self._starting[lang] = ev
            try:
                client = self._spawn(lang, spec)
                self._clients[lang] = client
            except RuntimeError as e:
                raise ValueError(str(e))
            finally:
                ev.set()
                self._starting.pop(lang, None)
        else:
            ev.wait()
            client = self._clients[lang]
        return client

    def _spawn(self, lang: str, spec: ServerSpec) -> LSPClient:
        # language_id per extension is applied at didOpen; the client itself is
        # keyed by language, and ensure_synced passes the file's language id
        # resolved from spec.extension_to_language. First extension's language
        # id is used when the file's exact id is unknown.
        first_lang = next(iter(spec.extension_to_language.values()), lang)
        client = LSPClient(spec, language_id=first_lang, root=self.ws_root, sandbox=self._sandbox)
        if not client.start():
            raise RuntimeError(f"failed to start language server {spec.command}")
        return client

    def _prepare(self, file: str, line: int, symbol: str) -> tuple[LSPClient, str, dict]:
        path = self._abs(file)
        client = self.resolve(path)
        uri = _path_to_uri(path)
        client.ensure_synced(uri, path)
        content = Path(path).read_text(encoding="utf-8", errors="replace")
        lines = content.split("\n")
        if line < 1 or line > len(lines):
            raise ValueError(f"line {line} out of range")
        col = _locate_symbol(lines[line - 1], symbol, client.pos_encoding)
        return client, uri, {"line": line - 1, "character": col}

    def definition(self, file: str, line: int, symbol: str) -> str:
        try:
            client, uri, pos = self._prepare(file, line, symbol)
            raw = client.request("textDocument/definition", {
                "textDocument": {"uri": uri}, "position": pos,
            })
            if raw is None:
                locs: list[dict] = []
            elif isinstance(raw, list):
                locs = [loc for loc in raw if isinstance(loc, dict)]
            else:
                locs = [raw]
            return _format_locations("definition", locs, self.ws_root)
        except ValueError as e:
            return f"Error: {e}"
        except RuntimeError as e:
            return f"Error: {e}"

    def references(self, file: str, line: int, symbol: str) -> str:
        try:
            client, uri, pos = self._prepare(file, line, symbol)
            raw = client.request("textDocument/references", {
                "textDocument": {"uri": uri}, "position": pos,
                "context": {"includeDeclaration": True},
            })
            return _format_locations("reference", raw if isinstance(raw, list) else [], self.ws_root)
        except ValueError as e:
            return f"Error: {e}"
        except RuntimeError as e:
            return f"Error: {e}"

    def hover(self, file: str, line: int, symbol: str) -> str:
        try:
            client, uri, pos = self._prepare(file, line, symbol)
            raw = client.request("textDocument/hover", {
                "textDocument": {"uri": uri}, "position": pos,
            })
            return _format_hover(raw)
        except ValueError as e:
            return f"Error: {e}"
        except RuntimeError as e:
            return f"Error: {e}"

    def document_symbol(self, file: str) -> str:
        path = self._abs(file)
        try:
            client = self.resolve(path)
        except ValueError as e:
            return f"Error: {e}"
        uri = _path_to_uri(path)
        client.ensure_synced(uri, path)
        try:
            raw = client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        except RuntimeError as e:
            return f"Error: {e}"
        symbols = raw if isinstance(raw, list) else []
        if not symbols:
            return "no document symbols found"
        out = []
        for s in symbols:
            name = s.get("name", "")
            kind = s.get("kind", "")
            if name:
                out.append(f"{name}" + (f" (kind {kind})" if kind else ""))
        return "\n".join(out[:_MAX_LOCATIONS])

    def workspace_symbol(self, query: str) -> str:
        results: list[str] = []
        for lang, client in list(self._clients.items()):
            try:
                raw = client.request("workspace/symbol", {"query": query})
            except RuntimeError:
                continue
            if isinstance(raw, list):
                for s in raw:
                    name = s.get("name", "")
                    uri = (s.get("location") or {}).get("uri", "")
                    if name and uri:
                        results.append(f"{name}  {_uri_to_path(uri) if uri.startswith('file://') else uri}")
        if not results:
            return "no workspace symbols found"
        return "\n".join(results[:_MAX_LOCATIONS])

    def shutdown(self) -> None:
        for client in list(self._clients.values()):
            try:
                client.shutdown()
            except Exception:
                pass
        self._clients.clear()




