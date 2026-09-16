"""Project trust gate — may this working directory's config be loaded?

A cloned repository can ship ``<cwd>/.cluxmate/``: ``settings.json`` runs shell
hooks, ``mcp.json`` spawns subprocesses, ``lsp.json`` can run installers,
``skills/`` + ``agents/`` reach the model, and ``permissions.json`` pre-authorizes
tool calls. All of it used to be read on sight, i.e. opening a repository was
enough to execute its code. Every one of those readers now takes a ``trusted``
flag derived from this module.

The decision is per directory and user-global (``~/.cluxmate/trust.json`` — inside
the WriteFence's non-writable ``~/.cluxmate`` tree, so a prompt-injected model
cannot grant itself trust). Precedence: per-run override (a front-end's
"this session only") > registry > ``unknown``. ``unknown`` and ``denied`` load
exactly the same thing (nothing project-level); only ``unknown`` is worth asking
about, and ``findings`` says whether there is anything to ask about at all.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TRUSTED = "trusted"
DENIED = "denied"
UNKNOWN = "unknown"
_STATUSES = (TRUSTED, DENIED)

_STATE_DIR = ".cluxmate"

# Config families under <cwd>/.cluxmate/ a repository can ship. Presence is the
# trigger — the content is NOT parsed: a file the repository put there is a file
# the user should be asked about, and a parse-based rule would silently stop
# asking the moment a schema changes.
_FINDING_FILES: tuple[tuple[str, str, str], ...] = (
    ("hooks", "Hooks (settings.json)", "settings.json"),
    ("mcp", "MCP servers (mcp.json)", "mcp.json"),
    ("lsp", "LSP servers (lsp.json)", "lsp.json"),
    ("skills", "Skills (skills.json)", "skills.json"),
    ("permissions", "Always-allow rules (permissions.json)", "permissions.json"),
)
_FINDING_GLOBS: tuple[tuple[str, str, str], ...] = (
    ("skills", "Skills (skills/*/SKILL.md)", "skills/*/SKILL.md"),
    ("agents", "Subagents (agents/*.md)", "agents/*.md"),
    ("facts", "Project memory facts (memory/facts/*.md)", "memory/facts/*.md"),
)


@dataclass(frozen=True)
class Finding:
    """One project-level config family this directory ships."""

    kind: str
    label: str
    path: str  # relative to the working directory

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "label": self.label, "path": self.path}


def canonical(cwd: str) -> str:
    """Absolute, symlink-resolved registry key for a working directory.

    Resolving means a symlink to a trusted repository is the same directory (no
    bypass), and ``.`` / trailing separators never create a second entry.
    """
    try:
        return str(Path(cwd).resolve())
    except (OSError, RuntimeError):
        # Deleted, unreadable or symlink-looped path: degrade to the absolute form
        # instead of raising (this runs on the front-ends' startup probe).
        try:
            return str(Path(cwd).absolute())
        except OSError:  # no cwd to be absolute against — the raw path is all we have
            return str(cwd)


def project_findings(cwd: str) -> list[Finding]:
    """Config families ``<cwd>/.cluxmate/`` ships, in a stable order.

    Runtime artifacts CluxMate itself writes there (``sandbox-il-applied``,
    ``tmp-low/``, ``tmp-spill/``) are deliberately NOT triggers — they are the
    product's own scratch, present in every sandboxed workspace.
    """
    base = Path(cwd) / _STATE_DIR
    found: dict[str, Finding] = {}
    for kind, label, name in _FINDING_FILES:
        try:  # an unreadable .cluxmate is "nothing found", not an error
            present = (base / name).is_file()
        except OSError:
            present = False
        if present:
            found[kind] = Finding(kind, label, f"{_STATE_DIR}{os.sep}{name}")
    for kind, label, pattern in _FINDING_GLOBS:
        if kind in found:
            continue
        try:
            present = next(iter(sorted(base.glob(pattern))), None) is not None
        except OSError:
            present = False
        if present:
            found[kind] = Finding(kind, label, f"{_STATE_DIR}{os.sep}{pattern}")
    return list(found.values())


class TrustStore:
    """User-global trust registry plus the per-run overrides.

    Best-effort like every other store in the tree: a missing or corrupt file is
    an empty registry and a failed write is logged to stderr and swallowed, so a
    read-only home never breaks the agent. Overrides live in ``_session`` and are
    never written — that is what "trust for this run only" means.
    """

    def __init__(self, path: str | Path | None = None):
        self._path = (
            Path(path) if path is not None
            else Path.home() / ".cluxmate" / "trust.json"
        )
        self._folders = self._load()
        self._session: dict[str, str] = {}

    # ── registry ──────────────────────────────────────────────

    def _load(self) -> dict[str, str]:
        try:
            raw = json.loads(self._path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        folders = raw.get("folders")
        if not isinstance(folders, dict):
            return {}
        return {
            str(k): str(v) for k, v in folders.items() if str(v) in _STATUSES
        }

    def _key(self, cwd: str) -> str | None:
        """Existing registry key for ``cwd``, matched case/separator-insensitively.

        Windows paths are case-insensitive and both separators reach us from the
        three front-ends, so an exact-string lookup would ask a directory the user
        already answered for.
        """
        want = os.path.normcase(canonical(cwd))
        for key in self._folders:
            if os.path.normcase(key) == want:
                return key
        return None

    def registry_status(self, cwd: str) -> str:
        key = self._key(cwd)
        return self._folders[key] if key is not None else UNKNOWN

    def entries(self) -> dict[str, str]:
        return dict(self._folders)

    def set(self, cwd: str, status: str) -> None:
        if status not in _STATUSES:
            raise ValueError(f"invalid trust status: {status!r}")
        # Reuse an existing case/separator variant key, otherwise the same
        # directory ends up recorded twice (we would then read the stale one).
        key = self._key(cwd)
        self._folders[key if key is not None else canonical(cwd)] = status
        self._save()

    def remove(self, cwd: str) -> bool:
        key = self._key(cwd)
        if key is None:
            return False
        del self._folders[key]
        self._save()
        return True

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(
                    {"version": 1, "folders": self._folders},
                    indent=2,
                    ensure_ascii=False,
                ),
                "utf-8",
            )
        except OSError:
            traceback.print_exc(file=sys.stderr)

    # ── per-run overrides ─────────────────────────────────────

    def session_status(self, cwd: str) -> str:
        return self._session.get(os.path.normcase(canonical(cwd)), UNKNOWN)

    def set_session(self, cwd: str, status: str) -> None:
        if status not in _STATUSES:
            raise ValueError(f"invalid trust status: {status!r}")
        self._session[os.path.normcase(canonical(cwd))] = status

    def clear_session(self, cwd: str) -> None:
        """Drop this run's override for ``cwd``, if any.

        A persisted decision is the user's answer for the DIRECTORY; an override
        answers only this process. Keeping the override on top of a fresh
        registry write would let it shadow that write — a persisted ``denied``
        would still resolve as ``trusted`` for the rest of the run.
        """
        self._session.pop(os.path.normcase(canonical(cwd)), None)

    def status(self, cwd: str) -> str:
        return self.resolve(cwd)[0]

    def resolve(self, cwd: str) -> tuple[str, str]:
        """``(status, source)`` with ``source`` one of ``session|registry|default``."""
        override = self.session_status(cwd)
        if override != UNKNOWN:
            return override, "session"
        recorded = self.registry_status(cwd)
        if recorded != UNKNOWN:
            return recorded, "registry"
        return UNKNOWN, "default"


@dataclass(frozen=True)
class TrustDecision:
    """The resolved answer for one working directory, plus what it cost."""

    cwd: str
    status: str
    source: str
    findings: tuple[Finding, ...] = ()

    @property
    def trusted(self) -> bool:
        return self.status == TRUSTED

    @property
    def gated(self) -> bool:
        """Project config is being withheld here — the model/user should be told."""
        return not self.trusted and bool(self.findings)

    @property
    def pending(self) -> bool:
        """Undecided AND there is something to decide about — i.e. ask."""
        return self.status == UNKNOWN and bool(self.findings)

    def as_dict(self) -> dict[str, Any]:
        return {
            "cwd": self.cwd,
            "status": self.status,
            "source": self.source,
            "findings": [f.as_dict() for f in self.findings],
        }


def resolve_trust(cwd: str, store: TrustStore | None = None) -> TrustDecision:
    """Resolve the trust decision for ``cwd`` (registry lookup + findings probe)."""
    st = store if store is not None else TrustStore()
    status, source = st.resolve(cwd)
    return TrustDecision(
        cwd=canonical(cwd),
        status=status,
        source=source,
        findings=tuple(project_findings(cwd)),
    )
