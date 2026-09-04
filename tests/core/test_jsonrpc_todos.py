"""Tests for the session/todos JSON-RPC host method (plan-strip replay)."""

from cluxmate.core.jsonrpc_server import JsonRpcServer
from cluxmate.core.session_log import SessionHeader, SessionLog
from cluxmate.core.session_log_store import SessionLogStore


def _header(sid: str) -> SessionHeader:
    return SessionHeader(id=sid, createdAt=0, apiType="openai")


def _server_with_session(tmp_path, todos):
    s = JsonRpcServer()
    store = SessionLogStore(str(tmp_path / "sessions"))
    store.create(_header("s1"))
    log = SessionLog.create(_header("s1"))
    log.append("todo/write", {"todos": todos})
    store.append("s1", log.events)
    s._log_store = store
    return s


def test_todos_folded_last_write_wins(tmp_path):
    s = _server_with_session(tmp_path, [
        {"content": "a", "status": "completed"},
        {"content": "b", "status": "in_progress"},
    ])
    assert s._session_todos("s1") == [
        {"content": "a", "status": "completed"},
        {"content": "b", "status": "in_progress"},
    ]


def test_unknown_session_returns_none(tmp_path):
    s = _server_with_session(tmp_path, [{"content": "a", "status": "pending"}])
    assert s._session_todos("missing") is None


def test_no_todo_events_returns_none(tmp_path):
    s = JsonRpcServer()
    store = SessionLogStore(str(tmp_path / "sessions"))
    store.create(_header("plain"))
    s._log_store = store
    assert s._session_todos("plain") is None


def test_empty_sid_returns_none(tmp_path):
    s = _server_with_session(tmp_path, [{"content": "a", "status": "pending"}])
    assert s._session_todos("") is None
