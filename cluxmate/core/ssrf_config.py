"""SSRF network-access config — "允许访问的主机" store.

The writable-grants registry (core/grants.py) answers "which folders may the
sandbox WRITE". This is the network sibling: "which hosts may web_fetch /
web_search reach, and which extra ranges to block". Unlike grants, the config
is re-read on EVERY request (mtime-cached) so a change takes effect immediately
without killing the bridge — the desktop Settings writes the file directly and
the next web_fetch picks it up.

Schema (JSON at ~/.cluxmate/ssrf.json):
    {"allow": ["localhost:3000", "10.0.0.0/8"], "block_extra": ["203.0.113.0/24"]}

Rules (mirroring GrantStore for consistency):
- Entries are host / host:port / [ipv6]:port / IP / CIDR (see _ssrf.parse_entry).
- Best-effort I/O: a missing/corrupt file yields an empty policy; a failed
  write is swallowed (logged) so a read-only home never breaks the agent.
- The file lives in ~/.cluxmate/, outside the WriteFence writable roots, so the
  model's file tools cannot edit its own network allowlist.
"""

from __future__ import annotations

import json
import sys
import threading
import traceback
from pathlib import Path

from cluxmate.tools._ssrf import parse_entry


class SsrConfig:
    """Reads/writes ~/.cluxmate/ssrf.json (one policy per user)."""

    def __init__(self, path: Path | None = None):
        if path is None:
            path = Path.home() / ".cluxmate" / "ssrf.json"
        self._path = Path(path)
        self._lock = threading.Lock()
        self._allow: list[str] = []
        self._block_extra: list[str] = []
        self._sig: tuple[int, int] | None = None
        self._load()
        self._sig = self._stat()

    def _stat(self) -> tuple[int, int] | None:
        try:
            st = self._path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        clean = (
            lambda xs: []
            if not isinstance(xs, list)
            else [s for s in xs if isinstance(s, str) and s.strip()]
        )
        self._allow = clean(data.get("allow", []) or [])
        self._block_extra = clean(data.get("block_extra", []) or [])

    def _save_locked(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(
                    {"allow": self._allow, "block_extra": self._block_extra},
                    indent=2,
                    ensure_ascii=False,
                ),
                "utf-8",
            )
        except OSError:
            traceback.print_exc(file=sys.stderr)

    def snapshot(self) -> dict[str, list[str]]:
        """Current policy (copy). Re-reads the file when its mtime/size changed."""
        with self._lock:
            sig = self._stat()
            if sig != self._sig:
                self._sig = sig
                self._load()
            return {"allow": list(self._allow), "block_extra": list(self._block_extra)}

    def set_rules(self, allow: list[str], block_extra: list[str]) -> dict[str, list[str]]:
        """Validate + persist a new policy. Raises ValueError on invalid entries.
        Returns the canonical snapshot after the write."""
        entries = list(allow) + list(block_extra)
        for e in entries:
            if parse_entry(e) is None:
                raise ValueError(f"Invalid network-access entry: {e!r}")
        with self._lock:
            self._allow = [e for e in allow if e.strip()]
            self._block_extra = [e for e in block_extra if e.strip()]
            self._save_locked()
            self._sig = self._stat()
            return {"allow": list(self._allow), "block_extra": list(self._block_extra)}
