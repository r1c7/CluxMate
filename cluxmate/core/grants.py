"""Persistent writable-folder grants — the "特许访问文件夹" registry.

This is the single source of truth for which folders the sandboxed surfaces
(bash / MCP subprocesses, and — for consistency — the file tools) may WRITE
outside the implicit working directory.

Why a registry (not just labels on disk): a Low-IL label is an *enforcement
artifact*, not a permission. To RESTORE a folder from Low → Medium when the
user revokes access, you must know which folders you ever labeled. This store
remembers that, so revocation is a precise reconcile (re-label the removed
path medium) rather than a whole-disk scan.

Schema (JSON at ~/.cluxmate/sandbox-grants.json):
    {"paths": ["D:/data", "E:/assets", ...]}

Rules:
- Paths are stored ABSOLUTE and resolved (the store normalizes on write).
- cwd is NOT stored — it is implicitly writable (方案 1). Only EXTRA folders
  the user explicitly granted live here.
- The platform temp dir and the global memory file (~/.cluxmate/AGENTS.md)
  are fence-side concerns, not grants — they stay hardcoded in the fence.
- Best-effort I/O, mirroring PermissionStore: a missing/corrupt file yields an
  empty grant set; a failed write is swallowed (logged) so a read-only home
  never breaks the agent.
"""

from __future__ import annotations

import json
import sys
import threading
import traceback
from pathlib import Path


class GrantStore:
    """Reads/writes ~/.cluxmate/sandbox-grants.json (one registry per user).

    Thread-safe: a mutex guards in-memory state + write-through. Callers share
    one instance per process (built once in the builder / jsonrpc server).
    """

    def __init__(self, root: Path | None = None):
        if root is None:
            root = Path.home() / ".cluxmate"
        self._path = Path(root) / "sandbox-grants.json"
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
        """Current granted absolute paths (copy)."""
        with self._lock:
            return list(self._paths)

    def add(self, path: str) -> str:
        """Grant a folder. Returns its normalized absolute path.

        Idempotent: an already-granted path is returned unchanged.
        """
        resolved = str(Path(path).resolve())
        with self._lock:
            if resolved not in self._paths:
                self._paths.append(resolved)
                self._save_locked()
            return resolved

    def remove(self, path: str) -> str | None:
        """Revoke a folder. Returns the removed absolute path, or None if it
        wasn't granted. The CALLER is responsible for the enforcement-side
        reconcile (restore Low → Medium) — this store only forgets."""
        resolved = str(Path(path).resolve())
        with self._lock:
            if resolved in self._paths:
                self._paths.remove(resolved)
                self._save_locked()
                return resolved
            # Also accept a non-resolved form that resolves to a granted path.
            for p in self._paths:
                if p == resolved:
                    self._paths.remove(p)
                    self._save_locked()
                    return p
        return None
