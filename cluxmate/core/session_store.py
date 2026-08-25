"""SessionStore — SQLite metadata + JSONL event-log persistence.

Session *metadata* (id, title, provider, model, cwd, group, pin, message_count)
stays in SQLite at ``<root>/cluxmate.db`` — the desktop and TUI share this schema.
Conversation *history* is now an append-only JSONL event log at
``<root>/sessions/<id>.jsonl`` (see :class:`~cluxmate.core.session_log_store.SessionLogStore`),
replacing the legacy ``<id>.json`` message-file format (D6: no migration).
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cluxmate.core.checkpoints import delete_shadow_repo_for_cwd
from cluxmate.core.session_log import SURFACE_EVENT_TYPES, SessionHeader, SessionLog
from cluxmate.core.session_log_store import (
    SessionLogStore,
    SessionNotFoundError,
    subagent_session_ids,
)


def _same_cwd(a: str, b: str) -> bool:
    """Whether two working directories are the same project.

    The shadow repo is keyed by the *resolved* absolute path, so sessions that
    stored the same directory with different spellings (relative vs absolute,
    trailing separator, symlink) must still count as one project. Mirror that
    normalization here rather than comparing raw strings.
    """
    if not a or not b:
        return False
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return a == b


class SessionStore:
    """Manages session metadata in SQLite and history as a JSONL event log.

    ``root_dir`` defaults to ``~/.cluxmate``; pass an explicit path in tests so
    both the SQLite db and the ``sessions/`` directory live under one temp root.
    """

    def __init__(self, root_dir: str | Path | None = None):
        self._root = Path(root_dir) if root_dir is not None else Path.home() / ".cluxmate"
        self._session_dir = self._root / "sessions"
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._root / "cluxmate.db"
        self._log_store = SessionLogStore(self._session_dir)
        self._conn: sqlite3.Connection | None = None

    @property
    def log_store(self) -> SessionLogStore:
        """The JSONL event-log store (exposed so builders can persist subagent logs)."""
        return self._log_store

    # ── database connection ──────────────────────────────────────────

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path))
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.row_factory = sqlite3.Row
            self._init_schema()
        return self._conn

    def _init_schema(self):
        """Create tables at the final schema, then apply additive migrations."""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id            TEXT PRIMARY KEY,
                title         TEXT NOT NULL DEFAULT 'New Session',
                provider      TEXT NOT NULL,
                model         TEXT NOT NULL,
                model_id      TEXT,
                api_type      TEXT,
                reasoning_effort TEXT,
                cwd           TEXT NOT NULL,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL,
                message_count INTEGER DEFAULT 0,
                group_id      TEXT REFERENCES groups(id) ON DELETE SET NULL,
                is_pinned     INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS groups (
                id         TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                created_at TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                is_auto    INTEGER NOT NULL DEFAULT 0,
                path       TEXT
            );
        """)
        self.conn.commit()
        # v6: auto groups are keyed by their RESOLVED path, not name. The CREATE
        # TABLE above covers fresh DBs; ALTER here covers an existing table.
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(groups)").fetchall()}
        if "path" not in cols:
            self.conn.execute("ALTER TABLE groups ADD COLUMN path TEXT")
        # Backfill `path` for pre-existing auto groups from their first session's
        # cwd. Idempotent: only rows still lacking a path are touched.
        for row in self.conn.execute(
            "SELECT id FROM groups WHERE is_auto = 1 AND path IS NULL"
        ).fetchall():
            sess = self.conn.execute(
                "SELECT cwd FROM sessions WHERE group_id = ? ORDER BY updated_at ASC LIMIT 1",
                (row["id"],),
            ).fetchone()
            if sess and sess["cwd"]:
                resolved = os.path.realpath(sess["cwd"])
                self.conn.execute(
                    "UPDATE groups SET path = ? WHERE id = ?", (resolved, row["id"])
                )
        self.conn.commit()

    # ── group helpers (mirrors desktop session-store.ts) ────────────

    def _ensure_group_for_cwd(self, cwd: str) -> str | None:
        resolved = os.path.realpath(cwd) if cwd else ""
        name = os.path.basename(resolved)
        if not name:
            return None
        # Auto groups are keyed by their RESOLVED path (not name), so two
        # different directories that share a basename stay as separate projects.
        row = self.conn.execute(
            "SELECT id FROM groups WHERE is_auto = 1 AND path = ?", (resolved,)
        ).fetchone()
        if row:
            return row["id"]
        gid = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        max_order = self.conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) AS mx FROM groups"
        ).fetchone()["mx"]
        self.conn.execute(
            "INSERT INTO groups (id, name, created_at, sort_order, is_auto, path) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (gid, name, now, max_order + 1, resolved),
        )
        self.conn.commit()
        return gid

    def _cleanup_auto_group(self, group_id: str | None):
        if not group_id:
            return
        row = self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM sessions WHERE group_id = ?",
            (group_id,),
        ).fetchone()
        if row and row["cnt"] == 0:
            self.conn.execute(
                "DELETE FROM groups WHERE id = ? AND is_auto = 1",
                (group_id,),
            )
            self.conn.commit()

    # ── session CRUD ─────────────────────────────────────────────────

    def create(
        self,
        title: str,
        provider: str,
        model: str,
        working_dir: str,
        model_id: str = "",
        api_type: str = "",
        reasoning_effort: str | None = None,
    ) -> str:
        sid = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        group_id = self._ensure_group_for_cwd(working_dir) if working_dir else None
        self.conn.execute(
            """INSERT INTO sessions
               (id, title, provider, model, model_id, api_type, reasoning_effort,
                cwd, created_at, updated_at, message_count, group_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (sid, title or "New Session", provider, model, model_id, api_type,
             reasoning_effort, working_dir, now, now, group_id),
        )
        self.conn.commit()
        # Materialize the JSONL event log header (D6: no legacy <id>.json).
        self._log_store.create(SessionHeader(
            id=sid,
            createdAt=int(time.time() * 1000),
            cwd=working_dir or None,
            provider=provider,
            model=model,
            apiType=api_type,
        ))
        return sid

    def load(self, session_id: str) -> dict[str, Any] | None:
        """Return session metadata + derived messages as a dict, or None."""
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        meta = dict(row)
        meta["is_pinned"] = bool(meta.get("is_pinned", 0))
        meta["working_dir"] = meta.pop("cwd", "")
        meta["messages"] = self.load_messages(session_id)
        return meta

    def load_log(self, session_id: str) -> SessionLog | None:
        """Reconstruct the live event log for a session (None if absent)."""
        try:
            header, events = self._log_store.load(session_id)
            return SessionLog.from_events(header, events)
        except SessionNotFoundError:
            return None

    def load_messages(self, session_id: str) -> list[dict]:
        """Derived provider-native messages (system excluded) from the event log."""
        log = self.load_log(session_id)
        return log.derive_messages() if log else []

    def append_events(self, session_id: str, events: list) -> None:
        """Durably append a contiguous batch of events and refresh metadata."""
        if not events:
            return
        self._log_store.append(session_id, events)
        # Increment message_count by the number of appended surface events.
        # (Recompute-from-log would be needed once surface `replace` compaction
        # events land — Phase 3b — since those shadow rather than add messages.)
        added = sum(1 for e in events if e.type in SURFACE_EVENT_TYPES)
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "UPDATE sessions SET message_count = message_count + ?, updated_at = ? "
            "WHERE id = ?",
            (added, now, session_id),
        )
        self.conn.commit()

    def sync_metadata(self, session_id: str) -> None:
        """Refresh message_count + updated_at from the JSONL log (metadata only).

        History is now persisted incrementally (IncrementalPersister), so the TUI
        calls this once at turn end to keep the SQLite side — session-list counts
        and recency ordering — in sync without re-append writes. Recomputing
        message_count from the surface is also correct under compaction (replace
        events shrink the surface rather than adding to it).
        """
        log = self.load_log(session_id)
        count = len(log.surface) if log else 0
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "UPDATE sessions SET message_count = ?, updated_at = ? WHERE id = ?",
            (count, now, session_id),
        )
        self.conn.commit()

    def set_title_if_default(self, session_id: str, title: str) -> None:
        """Set the title only when it is still the default placeholder."""
        row = self.conn.execute(
            "SELECT title FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row and (not row["title"] or row["title"] == "New Session"):
            self.rename(session_id, title)

    def delete(self, session_id: str):
        # Record group + cwd before deleting so we can clean up auto-groups and
        # (when this was the last session in its directory) the shadow repo.
        row = self.conn.execute(
            "SELECT group_id, cwd FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        group_id = row["group_id"] if row else None
        cwd = row["cwd"] if row else None
        self.conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        self.conn.commit()
        if group_id:
            self._cleanup_auto_group(group_id)
        # Cascade-delete subagent logs (each is its own <child>.jsonl reachable
        # from the parent's spawn events) so deleting a session doesn't leak them.
        for child_id in subagent_session_ids(self._log_store, session_id):
            self._log_store.delete(child_id)
        self._log_store.delete(session_id)
        # The shadow repo is per-directory and shared across sessions. Only once
        # no session in this directory remains are its checkpoint SHAs
        # unreferenced and the repo safe to remove; otherwise leave it in place
        # (other sessions' undo anchors depend on the unchanged history).
        if cwd:
            remaining = [
                r["cwd"]
                for r in self.conn.execute("SELECT cwd FROM sessions").fetchall()
            ]
            if not any(_same_cwd(cwd, other) for other in remaining):
                delete_shadow_repo_for_cwd(cwd)

    def list_all(self) -> list[dict[str, Any]]:
        """Return all sessions ordered by pinned first, then most-recently-updated."""
        rows = self.conn.execute(
            "SELECT * FROM sessions ORDER BY is_pinned DESC, updated_at DESC"
        ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["is_pinned"] = bool(d.get("is_pinned", 0))
            d["working_dir"] = d.pop("cwd", "")  # TUI code expects working_dir
            d.pop("group_id", None)  # TUI doesn't use groups yet
            result.append(d)
        return result

    def list_groups(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM groups ORDER BY sort_order ASC"
        ).fetchall()
        return [
            {**dict(r), "is_auto": bool(r["is_auto"])}
            for r in rows
        ]

    def update_cwd(self, session_id: str, cwd: str):
        old = self.conn.execute(
            "SELECT group_id FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        old_group_id = old["group_id"] if old else None
        new_group_id = self._ensure_group_for_cwd(cwd) if cwd else None

        self.conn.execute(
            "UPDATE sessions SET cwd = ?, group_id = ?, updated_at = ? WHERE id = ?",
            (cwd, new_group_id, datetime.now(timezone.utc).isoformat(), session_id),
        )
        self.conn.commit()

        if old_group_id and old_group_id != new_group_id:
            self._cleanup_auto_group(old_group_id)

    def rename(self, session_id: str, title: str):
        self.conn.execute(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            (title, datetime.now(timezone.utc).isoformat(), session_id),
        )
        self.conn.commit()

    def set_reasoning_effort(self, session_id: str, effort: str | None):
        """Persist the session's selected reasoning level (None = provider default)."""
        self.conn.execute(
            "UPDATE sessions SET reasoning_effort = ?, updated_at = ? WHERE id = ?",
            (effort, datetime.now(timezone.utc).isoformat(), session_id),
        )
        self.conn.commit()

    def set_model(
        self, session_id: str, model_id: str, provider: str, model: str, api_type: str,
    ):
        """Persist the session's selected model (with denormalized display labels)."""
        self.conn.execute(
            "UPDATE sessions SET model_id = ?, provider = ?, model = ?, "
            "api_type = ?, updated_at = ? WHERE id = ?",
            (model_id, provider, model, api_type,
             datetime.now(timezone.utc).isoformat(), session_id),
        )
        self.conn.commit()

    def pin(self, session_id: str, pinned: bool):
        self.conn.execute(
            "UPDATE sessions SET is_pinned = ?, updated_at = ? WHERE id = ?",
            (1 if pinned else 0, datetime.now(timezone.utc).isoformat(), session_id),
        )
        self.conn.commit()
