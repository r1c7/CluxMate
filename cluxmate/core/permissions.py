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

Risk levels are safe < write < dangerous < critical. ``critical`` (device/system-
level destruction from bash — format/mkfs/dd/`> /dev/`/chmod 777) NEVER auto-
approves and is never always-allowable, exactly like sandbox escalation.

Always-allow is split across TWO risk tiers, both persisted:
    {"always_allow_tools": [str],             # write-tier: auto-approve writes
     "always_allow_dangerous_tools": [str]}   # dangerous-tier: auto-approve danger
- ``always_allow_tools`` covers a tool's *write* risk (write_file, bash npm
  install, …). This is the historical list; existing files keep working unchanged.
- ``always_allow_dangerous_tools`` covers a tool's *dangerous* risk. ``delete_file``
  is granted whole-tool (bounded by the WriteFence). ``bash`` is granted PER
  category — a destructive command (``"bash:rm"``, ``"bash:git-reset-hard"``) or a
  code runner (``"bash:python"``, ``"bash:node"``, fallback ``"bash:run"``) — a
  bare ``"bash"`` is NOT a valid grant, and a dangerous bash command auto-approves
  only when EVERY matched category is granted. Every other dangerous call — MCP
  dangerous tools, critical bash, and any sandbox escalation
  (``danger-full-access``) — NEVER auto-approves outside yolo.

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

# Dangerous tools that show an "always allow" button. delete_file is granted
# whole-tool (bounded by the WriteFence — deterministic canonicalize→deny→contain,
# single files only). bash is granted PER destructive category (``bash:<label>``),
# never whole-tool — the button names the specific category. Everything else
# dangerous — MCP tools configured as dangerous, critical bash, and any sandbox
# escalation — is never always-allowable.
ALWAYS_ALLOWABLE_DANGEROUS = frozenset({"delete_file", "bash"})


class PermissionStore:
    """Reads/writes <cwd>/.cluxmate/permissions.json. All ops are best-effort:
    a missing/corrupt file yields defaults, a failed write is swallowed (logged
    to stderr) so a read-only workspace never breaks the agent.

    Persists the two always-allow tiers; the development mode is per-session and
    intentionally not written here."""

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
            "always_allow_tools": _names(data.get("always_allow_tools")),
            "always_allow_dangerous_tools": _names(
                data.get("always_allow_dangerous_tools")
            ),
        }

    def save(self, always_allow_tools: list[str], always_allow_dangerous_tools: list[str]):
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(
                    {
                        "always_allow_tools": list(always_allow_tools),
                        "always_allow_dangerous_tools": list(
                            always_allow_dangerous_tools
                        ),
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                "utf-8",
            )
        except OSError:
            traceback.print_exc(file=sys.stderr)


def _names(value: Any) -> list[str]:
    """Coerce a JSON value into a list of non-empty strings."""
    if not isinstance(value, list):
        return []
    return [t for t in value if isinstance(t, str) and t]


class PermissionPolicy:
    """In-memory tool-approval policy for one session, backed by a PermissionStore
    scoped to the session's working directory. The two always-allow tiers are
    loaded at construction and written through on mutation; the development mode
    starts at DEFAULT_MODE and is never persisted.

    Project trust is the third input and it is NOT the same axis: an untrusted
    directory's rules are neither loaded nor written, because a grant nothing
    ever reads back is worse than no grant at all (see `mark_trusted` for the one
    way in, and `always_allow_verdict` for what that costs the UI)."""

    def __init__(self, cwd: str, *, trusted: bool = True):
        self._lock = threading.Lock()
        # Project trust gate (core/trust.py): an untrusted directory's
        # permissions.json is ignored AND must not be written — a
        # silently-inert "always allow" is worse than not offering one.
        self._trusted = trusted
        self._store = PermissionStore(cwd)
        state = self._store.load() if trusted else {
            "always_allow_tools": [],
            "always_allow_dangerous_tools": [],
        }
        self.mode: str = DEFAULT_MODE
        self.always_allow: set[str] = set(state["always_allow_tools"])
        self.always_allow_dangerous: set[str] = set(
            state["always_allow_dangerous_tools"]
        )

    @property
    def trusted(self) -> bool:
        with self._lock:
            return self._trusted

    def mark_trusted(self) -> None:
        """Trust this project for the rest of the session (the user consented).

        The approval card is the one place that asks about trust mid-turn: in a
        directory that ships no config there is nothing to prompt about, so
        "always allow" was refused forever — the grant that would create the
        config was exactly what trust gated. Answering both questions in the one
        click closes that loop, and the flip has to land BEFORE the write or the
        user is left with a rule that silently does not exist.

        On-disk rules are MERGED, never replaced: a directory that shipped its
        own permissions.json must not lose them to the flip. The mode is
        untouched — it is per-session and never persisted.
        """
        with self._lock:
            if self._trusted:
                return
            self._trusted = True
            state = self._store.load()
            self.always_allow.update(state["always_allow_tools"])
            self.always_allow_dangerous.update(state["always_allow_dangerous_tools"])

    def is_auto_approved(
        self,
        name: str,
        risk_level: str,
        escalated: bool = False,
        categories: frozenset[str] | None = None,
    ) -> bool:
        if risk_level == "safe":
            return True
        with self._lock:
            mode = self.mode
            # yolo: everything auto-approves, including dangerous (rm -rf, delete)
            # and sandbox escalation (the one explicit opt-out).
            if mode == "yolo":
                return True
            # Sandbox escalation (danger-full-access) bypasses the sandbox/fence:
            # it NEVER auto-approves outside yolo — the user approves each one.
            if escalated:
                return False
            if risk_level == "critical":
                # Device/system-level destruction (format/mkfs/dd/…): never
                # auto-approves, never "always-allow"-able — like escalation.
                return False
            if risk_level == "dangerous":
                if name == "bash":
                    # Category-scoped ONLY: auto-approve when every matched
                    # destructive category is granted. A bare "bash" grant is NOT
                    # honored — it must be per-category (bash:rm, bash:…).
                    return self._bash_dangerous_allowed(categories)
                # Other dangerous tools (delete_file) are granted whole-tool.
                return name in self.always_allow_dangerous
            # write-risk:
            # - plan never reaches here (no write tools are registered)
            # - acceptEdits auto-approves all writes
            # - default auto-approves only always-allowed tools
            if mode == "acceptEdits":
                return True
            return name in self.always_allow

    def _bash_dangerous_allowed(self, categories: frozenset[str] | None) -> bool:
        # Pure category-scoped: a bare "bash" grant is intentionally NOT honored
        # (see add_always_allow_dangerous — it rejects the bare name). This keeps
        # dangerous bash fine-grained; a command auto-approves only when EVERY
        # matched category has been explicitly granted.
        cats = categories or frozenset()
        if not cats:
            return False
        return all(f"bash:{c}" in self.always_allow_dangerous for c in cats)

    def always_allow_verdict(
        self,
        name: str,
        risk_level: str,
        escalated: bool = False,
        *,
        assume_trusted: bool = False,
    ) -> str:
        """Why "always allow" is (or is not) on offer for this call.

        The single source of truth behind :meth:`is_always_allowable`, and what
        the approval card renders from: ``untrusted`` is recoverable in the SAME
        click (the project can be trusted right there), while ``escalated`` /
        ``critical`` / ``not-grantable`` never become grantable in any directory.
        That difference is why this returns a reason instead of a bool — the UI
        must not infer "the project is the reason" from a bare ``False``.

        ``assume_trusted`` answers "would this be grantable IF the project were
        trusted?", which is what the approval path asks before it adopts the
        trust answer — otherwise the one call that needs to know could never be
        told apart from a call that is forbidden outright.
        """
        if escalated:
            return "escalated"
        if risk_level == "critical":
            return "critical"
        if risk_level == "safe":
            return "safe"
        if risk_level == "dangerous" and name not in ALWAYS_ALLOWABLE_DANGEROUS:
            return "not-grantable"
        if not (self._trusted or assume_trusted):
            return "untrusted"
        return "ok"

    def is_always_allowable(
        self,
        name: str,
        risk_level: str,
        escalated: bool = False,
        *,
        assume_trusted: bool = False,
    ) -> bool:
        """Whether the user may persist an "always allow" for THIS call.

        Drives the desktop's "总是允许" button: true for write tools and the
        dangerous tools in ALWAYS_ALLOWABLE_DANGEROUS; false for safe (never
        prompts), for critical (device/system-level destruction), for sandbox
        escalation, and for other dangerous tools.
        """
        return (
            self.always_allow_verdict(
                name, risk_level, escalated, assume_trusted=assume_trusted
            )
            == "ok"
        )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "mode": self.mode,
                # Derived for backward-compatible readers that still key on the
                # old boolean; acceptEdits is the mode that auto-approves writes.
                "accept_edits": self.mode == "acceptEdits",
                "always_allow_tools": sorted(self.always_allow),
                "always_allow_dangerous_tools": sorted(
                    self.always_allow_dangerous
                ),
                "trusted": self._trusted,
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
        self._store.save(
            sorted(self.always_allow),
            sorted(self.always_allow_dangerous),
        )

    def add_always_allow(self, name: str) -> bool:
        """Persist "always allow" at the tool's WRITE tier.

        Returns whether the rule is in force afterwards. ``False`` means nothing
        was written — the project is not trusted — and the caller must report an
        ordinary approval instead of a grant that does not exist.
        """
        with self._lock:
            if not self._trusted:
                return False
            if name in self.always_allow:
                return True
            self.always_allow.add(name)
            self._persist_locked()
            return True

    def add_always_allow_dangerous(self, name: str) -> bool:
        """Persist "always allow" at the tool's DANGEROUS tier.

        Accepts ``delete_file`` (whole-tool) or a bash category grant
        ``bash:<category>``. A bare ``bash`` is REJECTED — dangerous bash is
        granted per destructive category only, so a coarse whole-tool grant can
        never silently auto-approve every dangerous shell command. Category names
        are server-computed (the model never controls them), so a prefix check is
        sufficient here — a bogus ``bash:…`` entry is inert, never matching.

        Returns whether the rule is in force afterwards (same contract as
        :meth:`add_always_allow`); a rejected name is ``False`` too, because
        nothing was persisted either.
        """
        if name != "delete_file" and not name.startswith("bash:"):
            return False
        with self._lock:
            if not self._trusted:
                return False
            if name in self.always_allow_dangerous:
                return True
            self.always_allow_dangerous.add(name)
            self._persist_locked()
            return True
