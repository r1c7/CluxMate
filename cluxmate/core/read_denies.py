"""Persistent read-denylist registry — the "禁止读取文件夹" store.

The writable-grants registry (``core/grants.py``) answers "which folders may
the sandbox WRITE". This is its read-side sibling: "which folders may the
sandboxed surfaces NOT READ". It is a user-configured denylist intended to
hide secrets (~/.ssh, ~/.aws, .env, ...) from the model and from shell/MCP
subprocesses on the backends that can enforce it.

Schema (JSON at ~/.cluxmate/forbid-read.json):
    {
      "protect_sensitive": false,
      "paths": ["C:/Users/me/.ssh", "/home/me/.aws", ...]
    }

- ``paths`` — user additions, stored ABSOLUTE and resolved (the store
  normalizes on write).
- ``protect_sensitive`` — one-click switch for the BUILT-IN sensitive-file
  template (see below). Default false = zero behavior change: the template is
  opt-in because hiding credential files (e.g. every ``.env`` in the
  workspace) breaks legitimate edit/config workflows — mirrors Reasonix's
  ``[secrets] protect_sensitive_files``.

Built-in template (active only while ``protect_sensitive`` is true):

- **Pattern rules** (basename match, case-insensitive; enforced at the
  process level by ReadFence on ALL platforms): files named ``.env``,
  ``.git-credentials``, ``.netrc``, and any ``*.pem`` / ``*.key`` / ``*.p12``
  / ``*.pfx`` — anywhere on disk.
- **Fixed directories** (platform-aware): ``~/.ssh``, ``~/.aws``, plus
  ``~/.gnupg`` on POSIX / ``%APPDATA%\\gnupg`` on Windows. These are plain
  deny roots, so they join ``paths`` in ``effective_paths()`` and are also
  enforced shell-side by bwrap/Seatbelt.

Rules (mirroring GrantStore for consistency):
- cwd is NOT stored — the working directory is implicitly READABLE; only
  EXTRA folders the user explicitly hid live here.
- Best-effort I/O, mirroring PermissionStore/GrantStore: a missing/corrupt
  file yields an empty deny set; a failed write is swallowed (logged) so a
  read-only home never breaks the agent.

NOTE: the file tools enforce this at the process level (ReadFence, T1) on ALL
platforms. The shell sandbox can only enforce the PATH roots on Linux (bwrap)
and macOS (Seatbelt) — Windows Low-IL is write-only by construction, so the
shell-side deny is a documented no-op there (see _sandbox.py). The pattern
rules are process-level only everywhere.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from pathlib import Path

# Built-in sensitive-file template — modeled on Reasonix's
# protect_sensitive_files (internal/tool/builtin/confine.go::sensitiveReadPath).
# Basenames are compared lowercased (case-insensitive on every platform, same
# as the reference implementation).
SENSITIVE_BASENAMES = (".env", ".git-credentials", ".netrc")
SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx")


def sensitive_dir_defaults() -> list[str]:
    """Fixed sensitive directories, platform-aware and absolute.

    These are plain deny roots: when ``protect_sensitive`` is on they join the
    user's paths in ``ReadDenyStore.effective_paths()`` (and therefore the
    shell-side deny list on Linux/macOS). They do NOT depend on the directory
    existing — bwrap skips non-existent mounts, Seatbelt rules never match,
    and ReadFence containment against a missing path is a no-op.
    """
    dirs = [str(Path.home() / ".ssh"), str(Path.home() / ".aws")]
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            dirs.append(str(Path(appdata) / "gnupg"))
    else:
        dirs.append(str(Path.home() / ".gnupg"))
    return dirs


def is_sensitive_pattern(path: Path) -> bool:
    """True when ``path`` matches a built-in sensitive-file PATTERN rule.

    Pattern rules are basename-based (case-insensitive) and apply anywhere on
    disk — they cannot be expressed as a fixed deny root. The fixed-directory
    rules live in :func:`sensitive_dir_defaults` (plain roots). ``path``
    should already be resolved; non-existent paths are fine (resolution is
    the caller's job, e.g. ReadFence).
    """
    name = path.name.lower()
    if name in SENSITIVE_BASENAMES:
        return True
    return any(name.endswith(s) for s in SENSITIVE_SUFFIXES)


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
        self._paths: list[str] = []
        self._protect_sensitive = False
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        # Toggle: missing key (incl. legacy v1 files) → false.
        if data.get("protect_sensitive") is True:
            self._protect_sensitive = True
        paths = data.get("paths", [])
        if not isinstance(paths, list):
            return
        for p in paths:
            if isinstance(p, str) and p:
                try:
                    self._paths.append(str(Path(p).resolve()))
                except OSError:
                    continue

    def _save_locked(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(
                    {
                        "protect_sensitive": self._protect_sensitive,
                        "paths": self._paths,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                "utf-8",
            )
        except OSError:
            traceback.print_exc(file=sys.stderr)

    def snapshot(self) -> list[str]:
        """Current USER-CONFIGURED denied absolute paths (copy).

        Does NOT include the built-in template dirs — see ``effective_paths``.
        """
        with self._lock:
            return list(self._paths)

    def protect_sensitive(self) -> bool:
        """Whether the built-in sensitive-file template is enabled."""
        with self._lock:
            return self._protect_sensitive

    def set_protect_sensitive(self, enabled: bool) -> bool:
        """Enable/disable the built-in sensitive-file template (persisted)."""
        with self._lock:
            if self._protect_sensitive != enabled:
                self._protect_sensitive = enabled
                self._save_locked()
            return self._protect_sensitive

    def effective_paths(self) -> list[str]:
        """The full deny-root set: user paths + the built-in template dirs
        (when the toggle is on). This is what ReadFence and the shell sandbox
        receive; the pattern rules are matched separately in ReadFence."""
        with self._lock:
            paths = list(self._paths)
            if self._protect_sensitive:
                paths += sensitive_dir_defaults()
            return paths

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
