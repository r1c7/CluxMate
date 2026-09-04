"""Tests for fold_todos — the whole-value todo projection over the session log."""

from cluxmate.core.session_log import (
    SessionHeader,
    SessionLog,
    fold_todos,
)


def make_log() -> SessionLog:
    return SessionLog.create(SessionHeader(id="s1", createdAt=0, apiType="openai"))


def _write(log: SessionLog, todos: list[dict]) -> None:
    log.append("todo/write", {"todos": todos})


def test_empty_log_folds_none():
    assert fold_todos(make_log().events) is None


def test_last_write_wins():
    log = make_log()
    _write(log, [{"content": "a", "status": "pending"}])
    _write(
        log,
        [
            {"content": "a", "status": "completed"},
            {"content": "b", "status": "in_progress"},
        ],
    )
    assert fold_todos(log.events) == [
        {"content": "a", "status": "completed"},
        {"content": "b", "status": "in_progress"},
    ]


def test_turn_start_resets():
    log = make_log()
    _write(log, [{"content": "a", "status": "completed"}])
    log.append("turn/start", {"turn": 2})
    assert fold_todos(log.events) is None


def test_turn_start_then_write():
    log = make_log()
    _write(log, [{"content": "stale", "status": "completed"}])
    log.append("turn/start", {"turn": 2})
    _write(log, [{"content": "fresh", "status": "in_progress"}])
    assert fold_todos(log.events) == [{"content": "fresh", "status": "in_progress"}]


def test_other_events_ignored():
    log = make_log()
    _write(log, [{"content": "a", "status": "pending"}])
    log.append("turn/end", {"turn": 1, "reason": {}})
    log.append("request/header", {"header": {"config": {}}})
    log.append("step/start", {"turn": 1, "step": 1})
    assert fold_todos(log.events) == [{"content": "a", "status": "pending"}]


def test_todo_write_is_log_only_not_surface():
    """todo/write must never enter the model-visible surface: the model
    already knows what it declared, and the transcript must stay cache-stable."""
    log = make_log()
    log.append(
        "user/message",
        {"message": {"role": "user", "content": "hi"}, "source": "human"},
        surface_op="append",
    )
    _write(log, [{"content": "a", "status": "pending"}])
    # The surface has exactly the one message; the todo list rode a
    # log-only event instead.
    assert log.derive_messages() == [{"role": "user", "content": "hi"}]
    assert [e.type for e in log.events] == ["user/message", "todo/write"]
    assert log.events[1].data == {"todos": [{"content": "a", "status": "pending"}]}


def test_fold_from_state():
    log = make_log()
    log.append("turn/start", {"turn": 1})
    _write(log, [{"content": "a", "status": "pending"}])
    prior = [{"content": "x", "status": "completed"}]
    # from_state seeds the fold; the tail's todo/write replaces it.
    tail = log.events[1:]  # just the todo/write
    assert fold_todos(tail, from_state=prior) == [{"content": "a", "status": "pending"}]
    # A tail without any todo/write keeps the seeded state.
    assert fold_todos([], from_state=prior) == prior
