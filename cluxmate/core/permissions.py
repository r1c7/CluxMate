"""Project-scoped tool-approval policy + development mode.

Persisted at <cwd>/.cluxmate/permissions.json — same per-project location as
mcp.json and skills/, so "always allow X here" does NOT leak to other working
directories. Each desktop session is one Python process bound to one cwd, so the
policy is naturally per-session; storing it under the project root is what keeps
it from following the user to a different project.

The development *mode* is a separate axis from always-allow, and is deliberately
NOT persisted — every session starts in "default". This avoids a project silently
staying stuck in the all-permissive "yolo" mode across restarts.

Modes (see PermissionPolicy.is_auto_approved):
- "plan"        → read-only. The builder withholds every write tool, so there is
                  nothing to approve; writes can't happen at all (hard isolation).
- "default"     → safe auto-approves; write/dangerous prompt (unless always-allow).
- "acceptEdits" → safe + write auto-approve; dangerous still prompts.
- "yolo"        → everything auto-approves, INCLUDING dangerous (rm -rf, delete).

Persisted schema:
    {"always_allow_tools": [str]}
(An older schema also stored "accept_edits": bool; it is ignored on load.)
"""

from __future__ import annotations

import json
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

# Valid development modes, in the InputBox cycle order.
MODES = ("plan", "default", "acceptEdits", "yolo")
DEFAULT_MODE = "default"


class PermissionStore:
    """Reads/writes <cwd>/.cluxmate/permissions.json. All ops are best-effort:
    a missing/corrupt file yields defaults, a failed write is swallowed (logged
    to stderr) so a read-only workspace never breaks the agent.

    Only always_allow_tools is persisted — the development mode is per-session
    and intentionally not written here."""

    def __init__(self, cwd: str):
        self._path = Path(cwd) / ".cluxmate" / "permissions.json"

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        return {
            "always_allow_tools": [
                t for t in data.get("always_allow_tools", []) if isinstance(t, str) and t
            ],
        }

    def save(self, always_allow_tools: list[str]):
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(
                    {"always_allow_tools": list(always_allow_tools)},
                    indent=2,
                    ensure_ascii=False,
                ),
                "utf-8",
            )
        except OSError:
            traceback.print_exc(file=sys.stderr)


class PermissionPolicy:
    """In-memory tool-approval policy for one session, backed by a PermissionStore
    scoped to the session's working directory. always_allow_tools is loaded at
    construction and written through on mutation; the development mode starts at
    DEFAULT_MODE and is never persisted."""

    def __init__(self, cwd: str):
        self._lock = threading.Lock()
        self._store = PermissionStore(cwd)
        state = self._store.load()
        self.mode: str = DEFAULT_MODE
        self.always_allow: set[str] = set(state["always_allow_tools"])

    def is_auto_approved(self, name: str, risk_level: str) -> bool:
        if risk_level == "safe":
            return True
        with self._lock:
            mode = self.mode
            # yolo: everything auto-approves, including dangerous (rm -rf, delete).
            if mode == "yolo":
                return True
            if risk_level == "dangerous":
                # plan has no write tools to reach here; default/acceptEdits always
                # prompt for dangerous — only yolo (above) green-lights it.
                return False
            # write-risk:
            # - plan never reaches here (no write tools are registered)
            # - acceptEdits auto-approves all writes
            # - default auto-approves only always-allowed tools
            if mode == "acceptEdits":
                return True
            return name in self.always_allow

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "mode": self.mode,
                # Derived for backward-compatible readers that still key on the
                # old boolean; acceptEdits is the mode that auto-approves writes.
                "accept_edits": self.mode == "acceptEdits",
                "always_allow_tools": sorted(self.always_allow),
            }

    def set_mode(self, mode: str):
        if mode not in MODES:
            raise ValueError(f"invalid mode: {mode!r}")
        with self._lock:
            self.mode = mode  # not persisted — per-session only

    def set_accept_edits(self, value: bool):
        """Back-compat shim: the old boolean maps onto the mode axis. True →
        acceptEdits; False → default (but never downgrade plan/yolo silently)."""
        with self._lock:
            if value:
                self.mode = "acceptEdits"
            elif self.mode == "acceptEdits":
                self.mode = "default"

    def _persist_locked(self):
        self._store.save(sorted(self.always_allow))

    def add_always_allow(self, name: str):
        with self._lock:
            if name in self.always_allow:
                return
            self.always_allow.add(name)
            self._persist_locked()
