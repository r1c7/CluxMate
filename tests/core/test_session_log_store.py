"""Tests for the JSONL session-log persistence store (Phase 2)."""

import json
import os

import pytest

from cluxmate.core.session_log import (
    APPEND,
    ReplaceOp,
    SessionEvent,
    SessionHeader,
    SessionLog,
)
from cluxmate.core.session_log_store import (
    TOOL_OUTCOME_UNKNOWN,
    SessionLogCorruptionError,
    SessionLogStore,
    SessionLogStoreError,
    SessionNotFoundError,
)


def make_header(sid="s1", **kw) -> SessionHeader:
    base = {"id": sid, "createdAt": 1, "apiType": "openai"}
    base.update(kw)
    return SessionHeader(**base)


def _user(content: str) -> dict:
    return {"message": {"role": "user", "content": content}, "source": "human"}


def _assistant(content: str, tool_calls=None) -> dict:
    m = {"role": "assistant", "content": content}
    if tool_calls is not None:
        m["tool_calls"] = tool_calls
    return {"turn": 1, "step": 1, "message": m}


# ── roundtrip ───────────────────────────────────────────────────────────────


def test_roundtrip(tmp_path):
    store = SessionLogStore(tmp_path)
    store.create(make_header())
    log = SessionLog.create(make_header())
    log.append("user/message", _user("hi"), surface_op=APPEND)
    log.append("assistant/message", _assistant("hello"), surface_op=APPEND)
    store.append("s1", log.events)

    header, events = store.load("s1")
    assert header.id == "s1"
    assert [e.type for e in events] == ["user/message", "assistant/message"]
    assert events[0].surfaceOp == APPEND
    rebuilt = SessionLog.from_events(header, events)
    assert rebuilt.derive_messages() == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


def test_roundtrip_preserves_replace_surface_op(tmp_path):
    store = SessionLogStore(tmp_path)
    store.create(make_header())
    log = SessionLog.create(make_header())
    log.append("user/message", _user("hi"), surface_op=APPEND)
    log.append("assistant/message", _assistant("a"), surface_op=APPEND)
    log.append("assistant/message", _assistant("b"), surface_op=APPEND)
    log.append(
        "assistant/message",
        _assistant("[sum]"),
        surface_op=ReplaceOp(start=1, end=2),
        source_event_seqs=[1, 2],
    )
    store.append("s1", log.events)

    header, events = store.load("s1")
    rebuilt = SessionLog.from_events(header, events)
    assert [m["content"] for m in rebuilt.derive_messages()] == ["hi", "[sum]"]
    assert isinstance(events[-1].surfaceOp, ReplaceOp)
    assert events[-1].sourceEventSeqs == (1, 2)


def test_append_rejects_seq_gap(tmp_path):
    store = SessionLogStore(tmp_path)
    store.create(make_header())
    e0 = SessionEvent(seq=0, time=0, type="turn/start", data={"turn": 1})
    store.append("s1", [e0])
    e5 = SessionEvent(
        seq=5, time=0, type="turn/end", data={"turn": 1, "reason": {"kind": "completed"}}
    )
    with pytest.raises(SessionLogCorruptionError):
        store.append("s1", [e5])


# ── crash repair ────────────────────────────────────────────────────────────


def test_torn_tail_dropped_and_open_turn_closed(tmp_path):
    store = SessionLogStore(tmp_path)
    store.create(make_header())
    log = SessionLog.create(make_header())
    log.append("turn/start", {"turn": 1})
    log.append("user/message", _user("hi"), surface_op=APPEND)
    log.append("assistant/message", _assistant("do it"), surface_op=APPEND)
    store.append("s1", log.events)
    # Simulate a crash mid-write: a partial turn/end line with no newline.
    with open(store.path_for("s1"), "ab") as f:
        f.write(b'{"seq":3,"time":1,"type":"turn/end","data":{"turn":1')

    header, events = store.load("s1")
    assert events[-1].type == "turn/end"
    assert events[-1].data["reason"]["kind"] == "interrupted"
    # The torn line was durably truncated: a second load no longer sees a torn tail.
    _, events2 = store.load("s1")
    assert [e.type for e in events2][-1] == "turn/end"


def test_open_tool_call_repair_openai(tmp_path):
    store = SessionLogStore(tmp_path)
    store.create(make_header())
    log = SessionLog.create(make_header())
    log.append("turn/start", {"turn": 1})
    log.append("user/message", _user("hi"), surface_op=APPEND)
    log.append(
        "assistant/message",
        _assistant(
            "",
            tool_calls=[
                {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
            ],
        ),
        surface_op=APPEND,
    )
    log.append("tool/call", {"turn": 1, "step": 1, "callId": "c1", "name": "read_file", "input": {}})
    store.append("s1", log.events)

    header, events = store.load("s1")
    assert events[-2].type == "tool/result"
    assert events[-2].data["callId"] == "c1"
    assert events[-2].data["message"] == {
        "role": "tool",
        "tool_call_id": "c1",
        "content": TOOL_OUTCOME_UNKNOWN,
    }
    assert events[-1].type == "turn/end"
    assert events[-1].data["reason"]["kind"] == "interrupted"
    # The repaired transcript derives a balanced history.
    rebuilt = SessionLog.from_events(header, events)
    roles = [m["role"] for m in rebuilt.derive_messages()]
    assert roles == ["user", "assistant", "tool"]


def test_answered_tool_call_needs_no_result_closer(tmp_path):
    store = SessionLogStore(tmp_path)
    store.create(make_header())
    log = SessionLog.create(make_header())
    log.append("turn/start", {"turn": 1})
    log.append("user/message", _user("hi"), surface_op=APPEND)
    log.append("assistant/message", _assistant("", tool_calls=[{"id": "c1", "type": "function", "function": {"name": "r", "arguments": "{}"}}]), surface_op=APPEND)
    log.append("tool/call", {"turn": 1, "step": 1, "callId": "c1", "name": "r", "input": {}})
    log.append("tool/result", {"turn": 1, "step": 1, "callId": "c1", "message": {"role": "tool", "tool_call_id": "c1", "content": "ok"}}, surface_op=APPEND)
    # turn left open (no turn/end) but the call is answered
    store.append("s1", log.events)

    _, events = store.load("s1")
    assert events[-1].type == "turn/end"  # only the turn closer, no extra tool/result
    assert events[-2].type == "tool/result"  # the original answered result


def test_inspect_is_non_mutating(tmp_path):
    store = SessionLogStore(tmp_path)
    store.create(make_header())
    log = SessionLog.create(make_header())
    log.append("turn/start", {"turn": 1})
    log.append("user/message", _user("hi"), surface_op=APPEND)
    log.append("assistant/message", _assistant("do it"), surface_op=APPEND)
    store.append("s1", log.events)

    _, events = store.inspect("s1")
    assert events[-1].type == "assistant/message"  # no closers appended
    # Loading now repairs durably.
    _, loaded = store.load("s1")
    assert loaded[-1].type == "turn/end"


# ── self-heal: premature crash-repair closers ────────────────────────────────


def _write_raw(store, sid, header, events):
    """Write a session file line-by-line, bypassing the store's contiguity checks."""
    path = store.path_for(sid)
    lines = [json.dumps({"type": "session", **header.to_dict()}, ensure_ascii=False)]
    lines += [json.dumps(e, ensure_ascii=False) for e in events]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def test_load_drops_premature_interrupted_closer(tmp_path):
    """A load() that ran mid-turn inserted a synthetic turn/end; drop it.

    The file records: turn 1 started, then a spurious ``turn/end {interrupted}``
    (seq 3) the repair appended while the turn was streaming, then the live turn
    resumed at the same seq (assistant/message seq 3) and completed normally.
    """
    store = SessionLogStore(tmp_path)
    store.create(make_header())
    _write_raw(
        store,
        "s1",
        make_header(),
        [
            {"seq": 0, "time": 0, "type": "turn/start", "data": {"turn": 1}},
            {"seq": 1, "time": 0, "type": "user/message",
             "data": {"message": {"role": "user", "content": "hi"}, "source": "human"},
             "surfaceOp": "append"},
            {"seq": 2, "time": 0, "type": "step/start", "data": {"turn": 1, "step": 1}},
            {"seq": 3, "time": 1, "type": "turn/end",
             "data": {"turn": 1, "reason": {"kind": "interrupted", "stage": "streaming", "turn": 1, "step": 1}}},
            {"seq": 3, "time": 2, "type": "assistant/message",
             "data": {"turn": 1, "step": 1, "message": {"role": "assistant", "content": "hi back"}},
             "surfaceOp": "append"},
            {"seq": 4, "time": 3, "type": "turn/end",
             "data": {"turn": 1, "reason": {"kind": "completed"}}},
        ],
    )

    header, events = store.load("s1")
    assert [e.seq for e in events] == [0, 1, 2, 3, 4]
    assert [e.type for e in events] == [
        "turn/start", "user/message", "step/start", "assistant/message", "turn/end",
    ]
    assert events[-1].data["reason"]["kind"] == "completed"
    rebuilt = SessionLog.from_events(header, events)  # must not raise
    assert [m["content"] for m in rebuilt.derive_messages()] == ["hi", "hi back"]

    # Idempotent: a second load sees the cleaned file.
    _, events2 = store.load("s1")
    assert [e.seq for e in events2] == [0, 1, 2, 3, 4]


def test_load_drops_premature_closer_block(tmp_path):
    """A mid-tool-execution repair inserts tool/result + turn/end; drop both.

    The live turn had an open tool call when the repair ran, so the block is
    ``tool/result {TOOL_OUTCOME_UNKNOWN}`` + ``turn/end {interrupted}``; the live
    turn then wrote the real tool result and a real completed turn/end at the same
    seqs.
    """
    store = SessionLogStore(tmp_path)
    store.create(make_header())
    _write_raw(
        store,
        "s1",
        make_header(),
        [
            {"seq": 0, "time": 0, "type": "turn/start", "data": {"turn": 1}},
            {"seq": 1, "time": 0, "type": "user/message",
             "data": {"message": {"role": "user", "content": "hi"}, "source": "human"},
             "surfaceOp": "append"},
            {"seq": 2, "time": 0, "type": "assistant/message",
             "data": {"turn": 1, "step": 1, "message": {"role": "assistant", "content": "", "tool_calls": []}},
             "surfaceOp": "append"},
            {"seq": 3, "time": 0, "type": "tool/call",
             "data": {"turn": 1, "step": 1, "callId": "c1", "name": "read_file", "input": {}}},
            # spurious repair block
            {"seq": 4, "time": 1, "type": "tool/result",
             "data": {"turn": 1, "step": 1, "callId": "c1",
                      "message": {"role": "tool", "tool_call_id": "c1", "content": TOOL_OUTCOME_UNKNOWN},
                      "error": {"name": "interrupted", "code": "TOOL_OUTCOME_UNKNOWN"}},
             "surfaceOp": "append"},
            {"seq": 5, "time": 1, "type": "turn/end",
             "data": {"turn": 1, "reason": {"kind": "interrupted", "stage": "tool_executing", "turn": 1, "step": 1}}},
            # live turn resumed
            {"seq": 4, "time": 2, "type": "tool/result",
             "data": {"turn": 1, "step": 1, "callId": "c1",
                      "message": {"role": "tool", "tool_call_id": "c1", "content": "ok"}},
             "surfaceOp": "append"},
            {"seq": 5, "time": 3, "type": "turn/end",
             "data": {"turn": 1, "reason": {"kind": "completed"}}},
        ],
    )

    header, events = store.load("s1")
    assert [e.seq for e in events] == [0, 1, 2, 3, 4, 5]
    assert [e.type for e in events] == [
        "turn/start", "user/message", "assistant/message", "tool/call", "tool/result", "turn/end",
    ]
    assert events[-2].data["message"]["content"] == "ok"  # real result, not the marker
    rebuilt = SessionLog.from_events(header, events)
    assert [m["role"] for m in rebuilt.derive_messages()] == ["user", "assistant", "tool"]


def test_load_keeps_legitimate_interrupted_closer_at_eof(tmp_path):
    """A genuine crash-repair turn/end at end-of-file must be kept, not dropped."""
    store = SessionLogStore(tmp_path)
    store.create(make_header())
    _write_raw(
        store,
        "s1",
        make_header(),
        [
            {"seq": 0, "time": 0, "type": "turn/start", "data": {"turn": 1}},
            {"seq": 1, "time": 0, "type": "user/message",
             "data": {"message": {"role": "user", "content": "hi"}, "source": "human"},
             "surfaceOp": "append"},
            {"seq": 2, "time": 0, "type": "assistant/message",
             "data": {"turn": 1, "step": 1, "message": {"role": "assistant", "content": "doing"}},
             "surfaceOp": "append"},
            {"seq": 3, "time": 1, "type": "turn/end",
             "data": {"turn": 1, "reason": {"kind": "interrupted", "stage": "streaming", "turn": 1, "step": 1}}},
        ],
    )

    header, events = store.load("s1")
    assert [e.seq for e in events] == [0, 1, 2, 3]
    assert events[-1].type == "turn/end"
    assert events[-1].data["reason"]["kind"] == "interrupted"
    SessionLog.from_events(header, events)  # must not raise



# ── errors ──────────────────────────────────────────────────────────────────


def test_load_missing_raises_not_found(tmp_path):
    store = SessionLogStore(tmp_path)
    with pytest.raises(SessionNotFoundError):
        store.load("nope")


def test_load_rejects_header_id_mismatch(tmp_path):
    store = SessionLogStore(tmp_path)
    store.create(make_header(sid="s1"))
    os.replace(store.path_for("s1"), store.path_for("s2"))
    with pytest.raises(SessionLogCorruptionError):
        store.load("s2")


def test_create_rejects_duplicate(tmp_path):
    store = SessionLogStore(tmp_path)
    store.create(make_header())
    with pytest.raises(SessionLogStoreError):
        store.create(make_header())


def test_path_for_rejects_traversal(tmp_path):
    store = SessionLogStore(tmp_path)
    with pytest.raises(ValueError):
        store.path_for("../evil")
    with pytest.raises(ValueError):
        store.path_for("")
