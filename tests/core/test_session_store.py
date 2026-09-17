"""Tests for the SQLite-metadata + JSONL-event-log SessionStore."""

import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from cluxmate.core.session_log import APPEND, SessionLog
from cluxmate.core.session_store import SessionStore


@pytest.fixture
def store(tmp_path):
    return SessionStore(root_dir=tmp_path / ".cluxmate")


def _append_turn(log: SessionLog, turn: int, user_text: str, assistant_text: str) -> None:
    log.append("turn/start", {"turn": turn})
    log.append(
        "user/message",
        {"message": {"role": "user", "content": user_text}, "source": "human"},
        surface_op=APPEND,
    )
    log.append(
        "assistant/message",
        {"turn": turn, "step": 1, "message": {"role": "assistant", "content": assistant_text}},
        surface_op=APPEND,
    )
    log.append("turn/end", {"turn": turn, "reason": {"kind": "completed"}})


def _git(*args, cwd=None):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "-c", "init.defaultBranch=main", *args],
        cwd=cwd, check=True, capture_output=True,
    )


def _legacy_db(db_path: Path) -> None:
    """A pre-``project_root`` sessions table, i.e. what an existing user has."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE sessions (
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
            group_id      TEXT,
            is_pinned     INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE groups (
            id         TEXT PRIMARY KEY,
            name       TEXT NOT NULL,
            created_at TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_auto    INTEGER NOT NULL DEFAULT 0,
            path       TEXT
        );
        INSERT INTO sessions
            (id, title, provider, model, cwd, created_at, updated_at)
            VALUES ('old1', 'old', 'p', 'm', '/home/projects/legacy', '2020', '2020');
        """
    )
    conn.commit()
    conn.close()


class TestCreateAndLoad:
    def test_create_writes_sqlite_and_jsonl(self, store):
        sid = store.create("Test Session", "Anthropic", "claude-sonnet-5", "/home/test")
        assert len(sid) == 12

        row = store.conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (sid,)
        ).fetchone()
        assert row is not None
        assert row["title"] == "Test Session"
        assert row["provider"] == "Anthropic"
        assert row["model"] == "claude-sonnet-5"
        assert row["cwd"] == "/home/test"

        # JSONL event log exists (header only) — load_log reconstructs empty log.
        log = store.load_log(sid)
        assert log is not None
        assert log.derive_messages() == []
        assert store.load(sid)["messages"] == []

    def test_load_returns_working_dir_alias(self, store):
        sid = store.create("T", "P", "M", "/tmp/wd")
        assert store.load(sid)["working_dir"] == "/tmp/wd"

    def test_load_nonexistent(self, store):
        assert store.load("deadbeef1234") is None
        assert store.load_log("deadbeef1234") is None


class TestAppendEvents:
    def test_append_events_persists_and_derives(self, store):
        sid = store.create("S", "P", "M", "/tmp", api_type="openai")
        log = store.load_log(sid)
        _append_turn(log, 1, "hi", "hey")
        store.append_events(sid, log.events)

        row = store.conn.execute(
            "SELECT message_count FROM sessions WHERE id = ?", (sid,)
        ).fetchone()
        assert row["message_count"] == 2  # user + assistant

        assert store.load_messages(sid) == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hey"},
        ]

    def test_append_events_contiguous_across_turns(self, store):
        sid = store.create("S", "P", "M", "/tmp", api_type="openai")
        log = store.load_log(sid)
        _append_turn(log, 1, "a", "A")
        store.append_events(sid, log.events)
        # Second turn continues from the persisted seq.
        _append_turn(log, 2, "b", "B")
        store.append_events(sid, log.events[log.seq - 4:])  # only the new 4 events
        assert [m["content"] for m in store.load_messages(sid)] == ["a", "A", "b", "B"]


class TestDelete:
    def test_delete_removes_sqlite_and_jsonl(self, store):
        sid = store.create("Del", "P", "M", "/tmp")
        assert store.load(sid) is not None
        store.delete(sid)
        assert store.load(sid) is None
        assert store.load_log(sid) is None

    def test_delete_purges_shadow_repo_only_for_last_session(self, store, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(
            "cluxmate.core.session_store.delete_shadow_repo_for_cwd",
            lambda cwd: calls.append(cwd),
        )
        s1 = store.create("A", "P", "M", "/home/projects/shared")
        s2 = store.create("B", "P", "M", "/home/projects/shared")
        store.delete(s1)
        # s2 still shares the directory — the shadow repo must survive.
        assert calls == []
        store.delete(s2)
        # Now it was the last session in the directory — purge once.
        assert calls == ["/home/projects/shared"]

    def test_delete_treats_path_spelling_variants_as_same_dir(self, store, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(
            "cluxmate.core.session_store.delete_shadow_repo_for_cwd",
            lambda cwd: calls.append(cwd),
        )
        s1 = store.create("A", "P", "M", "/home/projects/shared/")
        s2 = store.create("B", "P", "M", "/home/projects/shared")
        store.delete(s1)
        # Trailing separator still resolves to the same directory.
        assert calls == []


class TestListAll:
    def test_list_all_ordered_by_updated(self, store):
        s1 = store.create("A", "P1", "M1", "/a")
        time.sleep(0.02)
        s2 = store.create("B", "P2", "M2", "/b")
        sessions = store.list_all()
        ids = [s["id"] for s in sessions]
        assert ids[0] == s2
        assert ids[1] == s1

    def test_list_all_pinned_first(self, store):
        s1 = store.create("A", "P1", "M1", "/a")
        s2 = store.create("B", "P2", "M2", "/b")
        store.pin(s1, True)
        sessions = store.list_all()
        assert [s["id"] for s in sessions][0] == s1


class TestGroups:
    def test_create_auto_groups_by_cwd(self, store):
        store.create("S1", "P", "M", "/home/projects/foo")
        store.create("S2", "P", "M", "/home/projects/foo")
        store.create("S3", "P", "M", "/home/projects/bar")
        names = {g["name"] for g in store.list_groups()}
        assert "foo" in names
        assert "bar" in names

    def test_cleanup_auto_group_on_delete(self, store):
        sid = store.create("S", "P", "M", "/home/projects/unique")
        assert any(g["name"] == "unique" for g in store.list_groups())
        store.delete(sid)
        assert not any(g["name"] == "unique" for g in store.list_groups())


class TestRenamePinCwd:
    def test_rename(self, store):
        sid = store.create("Old", "P", "M", "/tmp")
        store.rename(sid, "New Title")
        assert store.load(sid)["title"] == "New Title"

    def test_set_title_if_default(self, store):
        sid = store.create("New Session", "P", "M", "/tmp")
        store.set_title_if_default(sid, "First prompt")
        assert store.load(sid)["title"] == "First prompt"
        # Second call must NOT overwrite the already-set title.
        store.set_title_if_default(sid, "Second prompt")
        assert store.load(sid)["title"] == "First prompt"

    def test_pin_toggle(self, store):
        sid = store.create("P", "P", "M", "/tmp")
        store.pin(sid, True)
        assert store.load(sid)["is_pinned"] is True
        store.pin(sid, False)
        assert store.load(sid)["is_pinned"] is False

    def test_update_cwd(self, store):
        sid = store.create("C", "P", "M", "/old")
        store.update_cwd(sid, "/new")
        assert store.load(sid)["working_dir"] == "/new"


class TestProjectRoot:
    def test_worktree_session_shares_the_project_group(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        wt = repo / ".worktrees" / "me"
        wt.mkdir(parents=True)
        store = SessionStore(root_dir=tmp_path / "state")
        main_id = store.create("main", "p", "m", str(repo))
        wt_id = store.create("wt", "p", "m", str(wt), project_root=str(repo))
        auto = [r for r in store.list_groups() if r["is_auto"]]
        assert len(auto) == 1
        assert Path(auto[0]["path"]).resolve() == repo.resolve()
        assert store.load(main_id)["group_id"] == store.load(wt_id)["group_id"]
        assert store.load(wt_id)["project_root"] == str(repo)
        assert store.load(main_id)["project_root"] == str(repo)

    def test_deleting_a_worktree_session_keeps_the_main_shadow_repo(self, tmp_path):
        repo = tmp_path / "repo"
        wt = repo / ".worktrees" / "me"
        wt.mkdir(parents=True)
        store = SessionStore(root_dir=tmp_path / "state")
        store.create("main", "p", "m", str(repo))
        wt_id = store.create("wt", "p", "m", str(wt), project_root=str(repo))
        store.delete(wt_id)  # 只应清掉 worktree 自己的影子库
        # list_all() exposes the session cwd as `working_dir` (TUI contract).
        remaining = [r["working_dir"] for r in store.list_all()]
        assert remaining == [str(repo)]

    def test_deleting_a_worktree_session_purges_only_its_own_shadow_repo(
        self, tmp_path, monkeypatch
    ):
        calls: list[str] = []
        monkeypatch.setattr(
            "cluxmate.core.session_store.delete_shadow_repo_for_cwd",
            lambda cwd: calls.append(cwd),
        )
        repo = tmp_path / "repo"
        wt = repo / ".worktrees" / "me"
        wt.mkdir(parents=True)
        store = SessionStore(root_dir=tmp_path / "state")
        store.create("main", "p", "m", str(repo))
        wt_id = store.create("wt", "p", "m", str(wt), project_root=str(repo))
        store.delete(wt_id)
        # The shadow repo is per TREE, so only the worktree's own dir is purged —
        # the main worktree still has a session and keeps its shadow repo.
        assert calls == [str(wt)]

    def test_update_cwd_re_groups_a_session_by_its_project_root(self, tmp_path):
        repo = tmp_path / "repo"
        wt = repo / ".worktrees" / "me"
        wt.mkdir(parents=True)
        store = SessionStore(root_dir=tmp_path / "state")
        main_id = store.create("main", "p", "m", str(repo))
        sid = store.create("moved", "p", "m", str(tmp_path / "elsewhere"))

        store.update_cwd(sid, str(wt), project_root=str(repo))

        meta = store.load(sid)
        assert meta["working_dir"] == str(wt)  # the tree is still the session's
        assert meta["project_root"] == str(repo)
        assert meta["group_id"] == store.load(main_id)["group_id"]
        # The now-empty auto group of the abandoned directory is gone.
        assert [g["name"] for g in store.list_groups()] == ["repo"]

    @pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
    def test_plain_repo_subdirectory_session_keeps_its_own_group(self, tmp_path):
        """Only a LINKED worktree is grouped with its repo.

        A session in a subdirectory of an ordinary repo keeps its own directory
        (and so its own auto group), exactly as it did before worktree support.
        """
        repo = tmp_path / "repo"
        sub = repo / "pkg"
        sub.mkdir(parents=True)
        _git("init", cwd=repo)
        store = SessionStore(root_dir=tmp_path / "state")

        sid = store.create("s", "p", "m", str(sub))

        assert store.load(sid)["project_root"] == str(sub)
        assert [g["name"] for g in store.list_groups()] == ["pkg"]


class TestMigration:
    def test_an_existing_db_gains_the_column_additively(self, tmp_path):
        root = tmp_path / "state"
        _legacy_db(root / "cluxmate.db")

        store = SessionStore(root_dir=root)

        cols = {r[1] for r in store.conn.execute("PRAGMA table_info(sessions)")}
        assert "project_root" in cols
        # A pre-existing row keeps loading, with no project root recorded.
        assert store.load("old1")["project_root"] is None
        assert store.load("old1")["working_dir"] == "/home/projects/legacy"
        # Re-opening re-runs the migration: idempotent, not an error.
        again = SessionStore(root_dir=root)
        assert "project_root" in {
            r[1] for r in again.conn.execute("PRAGMA table_info(sessions)")
        }
        # A session created now still groups by its own directory (no worktree).
        sid = store.create("new", "p", "m", "/home/projects/legacy")
        assert store.load(sid)["group_id"] is not None
        assert [g["name"] for g in store.list_groups()] == ["legacy"]
