"""Persistent read-denylist registry — the "禁止读取文件夹" store.

The writable-grants registry (``core/grants.py``) answers "which folders may
the sandbox WRITE". This is its read-side sibling: "which folders may the
sandboxed surfaces NOT READ". It is a user-configured denylist (default empty
= zero behavior change) intended to hide secrets (~/.ssh, ~/.aws, .env, ...)
from the model and from shell/MCP subprocesses on the backends that can enforce
it.

Schema (JSON at ~/.cluxmate/forbid-read.json):
    {"paths": ["C:/Users/me/.ssh", "/home/me/.aws", ...]}

Rules (mirroring GrantStore for consistency):
- Paths are stored ABSOLUTE and resolved (the store normalizes on write).
- cwd is NOT stored — the working directory is implicitly READABLE; only
  EXTRA folders the user explicitly hid live here.
- Best-effort I/O, mirroring PermissionStore/GrantStore: a missing/corrupt
  file yields an empty deny set; a failed write is swallowed (logged) so a
  read-only home never breaks the agent.

NOTE: the file tools enforce this at the process level (ReadFence, T1) on ALL
platforms. The shell sandbox can only enforce it on Linux (bwrap) and macOS
(Seatbelt) — Windows Low-IL is write-only by construction, so the shell-side
deny is a documented no-op there (see _sandbox.py).
"""

from __future__ import annotations

import json
import sys
import threading
import traceback
from pathlib import Path


class ReadDenyStore:
    """Reads/writes ~/.cluxmate/forbid-read.json (one registry per user).

    Thread-safe: a mutex guards in-memory state + write-through. Callers share
    one instance per process (built once in the builder / jsonrpc server).
    """

    def __init__(self, root: Path | None = None):
        if root is None:
            root = Path.home() / ".cluxmate"
        self._path = Path(root) / "forbid-read.json"
        self._lock = threading.Lock()
        self._paths: list[str] = self._load()

    def _load(self) -> list[str]:
        try:
            data = json.loads(self._path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return []
        if not isinstance(data, dict):
            return []
        paths = data.get("paths", [])
        if not isinstance(paths, list):
            return []
        out: list[str] = []
        for p in paths:
            if isinstance(p, str) and p:
                try:
                    out.append(str(Path(p).resolve()))
                except OSError:
                    continue
        return out

    def _save_locked(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"paths": self._paths}, indent=2, ensure_ascii=False),
                "utf-8",
            )
        except OSError:
            traceback.print_exc(file=sys.stderr)

    def snapshot(self) -> list[str]:
        """Current denied absolute paths (copy)."""
        with self._lock:
            return list(self._paths)

    def add(self, path: str) -> str:
        """Deny a folder/file. Returns its normalized absolute path.

        Idempotent: an already-denied path is returned unchanged.
        """
        resolved = str(Path(path).resolve())
        with self._lock:
            if resolved not in self._paths:
                self._paths.append(resolved)
                self._save_locked()
            return resolved

    def remove(self, path: str) -> str | None:
        """Un-deny a folder/file. Returns the removed absolute path, or None if
        it wasn't denied. No enforcement-side reconcile is needed — a read deny
        leaves no on-disk label to restore (unlike the Low-IL write grants)."""
        resolved = str(Path(path).resolve())
        with self._lock:
            if resolved in self._paths:
                self._paths.remove(resolved)
                self._save_locked()
                return resolved
            # Also accept a non-resolved form that resolves to a denied path.
            for p in self._paths:
                if p == resolved:
                    self._paths.remove(p)
                    self._save_locked()
                    return p
        return None
