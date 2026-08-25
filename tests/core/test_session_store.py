"""Tests for the SQLite-metadata + JSONL-event-log SessionStore."""

import time

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
