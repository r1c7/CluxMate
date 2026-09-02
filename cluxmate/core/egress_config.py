"""Network-egress config — "bash/MCP 出网控制" store.

The SSRF config answers "which hosts may web_fetch/web_search reach". This is
the sibling for the shell sandbox: which egress mode the bash + MCP stdio
subprocesses run under. Mirroring SsrConfig, the file is re-read on snapshot
when its mtime/size changed.

Schema (JSON at ~/.cluxmate/egress.json):
    {"mode": "shared" | "off" | "proxy"}   (default "shared")

Rules:
- shared = current behavior (network unrestricted); off = kernel-level deny;
  proxy = force traffic through the local allowlist proxy.
- The mode is baked into the sandbox backend at build time, so a mode change
  requires an agent rebuild (the JSON-RPC egress/config/set does that).
- Best-effort I/O: a missing/corrupt file yields "shared"; a failed write is
  swallowed so a read-only home never breaks the agent.
"""

from __future__ import annotations

import json
import sys
import threading
import traceback
from pathlib import Path

_VALID_MODES = ("shared", "off", "proxy")


class EgressConfig:
    """Reads/writes ~/.cluxmate/egress.json (one policy per user)."""

    def __init__(self, path: Path | None = None):
        if path is None:
            path = Path.home() / ".cluxmate" / "egress.json"
        self._path = Path(path)
        self._lock = threading.Lock()
        self._mode = "shared"
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
        mode = data.get("mode", "shared")
        self._mode = mode if mode in _VALID_MODES else "shared"

    def _save_locked(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"mode": self._mode}, indent=2, ensure_ascii=False),
                "utf-8",
            )
        except OSError:
            traceback.print_exc(file=sys.stderr)

    def snapshot(self) -> dict[str, str]:
        """Current mode. Re-reads the file when its mtime/size changed."""
        with self._lock:
            sig = self._stat()
            if sig != self._sig:
                self._sig = sig
                self._load()
            return {"mode": self._mode}

    def set_mode(self, mode: str) -> dict[str, str]:
        """Validate + persist a new mode. Raises ValueError on invalid values."""
        if mode not in _VALID_MODES:
            raise ValueError(f"Invalid egress mode: {mode!r}")
        with self._lock:
            self._mode = mode
            self._save_locked()
            self._sig = self._stat()
            return {"mode": self._mode}
