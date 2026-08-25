"""Tests for the event-sourced session log (Phase 0/1)."""

import pytest

from cluxmate.core.session_log import (
    APPEND,
    SESSION_FORMAT_VERSION,
    ReplaceOp,
    SessionEvent,
    SessionHeader,
    SessionLog,
    canonical_header,
    diff_headers,
    fold_request_header,
    header_equals,
    is_json_value,
    snapshot_json_value,
)


def make_header(**kw) -> SessionHeader:
    base = {
        "id": "sess-1",
        "createdAt": 1700000000000,
        "provider": "openai",
        "model": "gpt-4o",
        "cwd": "/tmp/proj",
    }
    base.update(kw)
    return SessionHeader(**base)


def _user(content: str, source: str = "human") -> dict:
    return {"message": {"role": "user", "content": content}, "source": source}


def _assistant(content: str, turn: int = 1, step: int = 1) -> dict:
    return {"turn": turn, "step": step, "message": {"role": "assistant", "content": content}}


# ── lossless JSON ───────────────────────────────────────────────────────────


def test_snapshot_json_value_roundtrip_and_detach():
    src = {"a": [1, 2, {"b": None}], "c": True, "d": "x"}
    copy = snapshot_json_value(src)
    assert copy == src
    src["a"][2]["b"] = 99
    assert copy["a"][2]["b"] is None


def test_snapshot_json_value_rejects_non_json():
    for bad in (
        float("nan"),
        float("inf"),
        float("-inf"),
        b"bytes",
        (1, 2),
        {1: "non-string-key"},
        {object(): "x"},
        object(),
    ):
        with pytest.raises(ValueError):
            snapshot_json_value(bad)


def test_snapshot_json_value_rejects_cycles():
    x = []
    x.append(x)
    with pytest.raises(ValueError):
        snapshot_json_value(x)
    d = {}
    d["self"] = d
    with pytest.raises(ValueError):
        snapshot_json_value(d)


def test_is_json_value():
    assert is_json_value({"a": 1})
    assert is_json_value([None, True, 1.5, "s", {"k": []}])
    assert not is_json_value(float("inf"))
    assert not is_json_value({"bad": object()})


# ── header ──────────────────────────────────────────────────────────────────


def test_header_rejects_bad_version():
    with pytest.raises(ValueError):
        SessionHeader(id="s", createdAt=0, version=SESSION_FORMAT_VERSION + 1)
    with pytest.raises(ValueError):
        SessionHeader(id="", createdAt=0)
    with pytest.raises(ValueError):
        SessionHeader(id="s", createdAt=-1)
    with pytest.raises(ValueError):
        SessionHeader(id="s", createdAt=0, origin="nonsense")


# ── request header reconstruction ───────────────────────────────────────────


def test_canonical_header_drops_empty_system_and_tools():
    assert canonical_header({"config": {}, "system": "", "tools": []}) == {"config": {}}
    assert canonical_header({"config": {"m": 1}, "system": "s", "tools": [{"name": "t"}]}) == {
        "config": {"m": 1},
        "system": "s",
        "tools": [{"name": "t"}],
    }


def test_header_equals_fieldwise():
    a = {"config": {"provider": "openai", "model": "gpt-4o"}, "system": "you", "tools": [{"name": "r"}]}
    b = {"config": {"provider": "openai", "model": "gpt-4o"}, "system": "you", "tools": [{"name": "r"}]}
    assert header_equals(a, b)
    # different system
    assert not header_equals(a, {**a, "system": "other"})
    # different config
    assert not header_equals(a, {**a, "config": {"provider": "openai", "model": "gpt-4.1"}})
    # different tool order
    assert not header_equals(a, {**a, "tools": []})
    # canonical-empty system equals omitted system
    assert header_equals({"config": {}, "system": ""}, {"config": {}})


def test_fold_request_header_returns_latest():
    events = [
        SessionEvent(seq=0, time=0, type="turn/start", data={"turn": 1}),
        SessionEvent(
            seq=1,
            time=0,
            type="request/header",
            data={"header": {"config": {}, "system": "s1"}, "reason": "initial"},
        ),
        SessionEvent(
            seq=2,
            time=0,
            type="request/header",
            data={"header": {"config": {}, "system": "s2"}, "reason": "change"},
        ),
    ]
    assert fold_request_header(iter(events))["system"] == "s2"
    assert fold_request_header(iter([])) is None


# ── append / surface / derive ───────────────────────────────────────────────


def test_append_assigns_monotonic_seq():
    log = SessionLog.create(make_header())
    e1 = log.append("user/message", _user("hi"), surface_op=APPEND)
    e2 = log.append("assistant/message", _assistant("hello"), surface_op=APPEND)
    assert e1.seq == 0
    assert e2.seq == 1
    assert log.seq == 2


def test_derive_messages_projects_surface_in_order():
    log = SessionLog.create(make_header())
    log.append("user/message", _user("hi"), surface_op=APPEND)
    log.append("assistant/message", _assistant("hello"), surface_op=APPEND)
    log.append("tool/result", {"turn": 1, "step": 1, "message": {"role": "tool", "content": "r"}}, surface_op=APPEND)
    msgs = log.derive_messages()
    assert msgs == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "tool", "content": "r"},
    ]


def test_append_rejects_non_json_data():
    log = SessionLog.create(make_header())
    with pytest.raises(ValueError):
        log.append(
            "user/message",
            {"message": {"role": "user", "content": float("nan")}, "source": "human"},
            surface_op=APPEND,
        )


def test_surface_event_requires_surface_op():
    log = SessionLog.create(make_header())
    with pytest.raises(ValueError):
        log.append("user/message", _user("hi"))


def test_non_surface_event_rejects_surface_metadata():
    log = SessionLog.create(make_header())
    with pytest.raises(ValueError):
        log.append("turn/start", {"turn": 1}, surface_op=APPEND)


def test_surface_event_requires_message_field():
    log = SessionLog.create(make_header())
    with pytest.raises(ValueError):
        log.append("user/message", {"source": "human"}, surface_op=APPEND)


def test_replace_shadows_history_but_keeps_raw_log():
    log = SessionLog.create(make_header())
    log.append("user/message", _user("hi"), surface_op=APPEND)
    log.append("assistant/message", _assistant("first"), surface_op=APPEND)
    log.append("assistant/message", _assistant("second"), surface_op=APPEND)
    log.append(
        "assistant/message",
        _assistant("[summary]"),
        surface_op=ReplaceOp(start=1, end=2),
        source_event_seqs=[1, 2],
    )
    assert [m["content"] for m in log.derive_messages()] == ["hi", "[summary]"]
    assert len(log.events) == 4  # raw log retains the shadowed events


def test_sequential_replaces_shadow_prior_summary():
    """Two compactions in one session: the second can fold the first's summary,
    and its sourceEventSeqs records that summary's seq for traceability."""
    log = SessionLog.create(make_header())
    log.append("user/message", _user("task"), surface_op=APPEND)
    log.append("assistant/message", _assistant("a1"), surface_op=APPEND)
    log.append("assistant/message", _assistant("a2"), surface_op=APPEND)
    log.append("assistant/message", _assistant("a3"), surface_op=APPEND)

    # compaction 1: shadow a1 (surface index 1) with summary1
    log.append(
        "user/message",
        _user("[summary1]", source="compaction"),
        surface_op=ReplaceOp(start=1, end=1),
        source_event_seqs=[1],
    )
    # compaction 2: shadow summary1 (seq 4) + a2 (seq 2) with summary2
    log.append(
        "user/message",
        _user("[summary2]", source="compaction"),
        surface_op=ReplaceOp(start=1, end=2),
        source_event_seqs=[4, 2],
    )

    assert [m["content"] for m in log.derive_messages()] == ["task", "[summary2]", "a3"]
    assert log.seq == 6  # append-only: all six events retained in the raw log
    second = log.events[-1]
    assert second.sourceEventSeqs == (4, 2)  # shadows the first summary (4) + a2 (2)


def test_replace_requires_valid_range_and_source_seqs():
    log = SessionLog.create(make_header())
    log.append("user/message", _user("hi"), surface_op=APPEND)
    log.append("assistant/message", _assistant("first"), surface_op=APPEND)
    # missing source seqs
    with pytest.raises(ValueError):
        log.append("assistant/message", _assistant("x"), surface_op=ReplaceOp(start=1, end=1))
    # out of range
    with pytest.raises(ValueError):
        log.append(
            "assistant/message",
            _assistant("x"),
            surface_op=ReplaceOp(start=0, end=5),
            source_event_seqs=[0, 1],
        )


def test_source_preserved_on_user_message():
    log = SessionLog.create(make_header())
    e = log.append("user/message", _user("[项目记忆]", source="memory"), surface_op=APPEND)
    assert e.data["source"] == "memory"


# ── reconstruction ──────────────────────────────────────────────────────────


def test_from_events_rebuilds_surface_and_rejects_gap():
    events = [
        SessionEvent(seq=0, time=0, type="user/message", data=_user("hi"), surfaceOp=APPEND),
        SessionEvent(seq=1, time=0, type="assistant/message", data=_assistant("hello"), surfaceOp=APPEND),
    ]
    log = SessionLog.from_events(make_header(), events)
    assert [m["content"] for m in log.derive_messages()] == ["hi", "hello"]
    assert log.seq == 2

    gapped = [
        SessionEvent(seq=0, time=0, type="user/message", data=_user("hi"), surfaceOp=APPEND),
        SessionEvent(seq=2, time=0, type="assistant/message", data=_assistant("hello"), surfaceOp=APPEND),
    ]
    with pytest.raises(ValueError):
        SessionLog.from_events(make_header(), gapped)


def test_from_events_replays_replace():
    events = [
        SessionEvent(seq=0, time=0, type="user/message", data=_user("hi"), surfaceOp=APPEND),
        SessionEvent(seq=1, time=0, type="assistant/message", data=_assistant("first"), surfaceOp=APPEND),
        SessionEvent(seq=2, time=0, type="assistant/message", data=_assistant("second"), surfaceOp=APPEND),
        SessionEvent(
            seq=3,
            time=0,
            type="assistant/message",
            data=_assistant("[summary]"),
            surfaceOp=ReplaceOp(start=1, end=2),
            sourceEventSeqs=(1, 2),
        ),
    ]
    log = SessionLog.from_events(make_header(), events)
    assert [m["content"] for m in log.derive_messages()] == ["hi", "[summary]"]


# ── observers ───────────────────────────────────────────────────────────────


def test_observers_notified_and_failure_contained():
    log = SessionLog.create(make_header())
    seen = []

    def bad(_event):
        raise RuntimeError("boom")

    def good(event):
        seen.append(event.type)

    log.on_append(bad)
    dispose = log.on_append(good)
    log.append("user/message", _user("hi"), surface_op=APPEND)
    assert seen == ["user/message"]
    dispose()
    log.append("assistant/message", _assistant("hello"), surface_op=APPEND)
    assert seen == ["user/message"]  # disposed observer no longer fires


# ── change delta (diff_headers / turn_changes) ──────────────────────────────


def _hdr(config=None, system="s", tools=None):
    return {"config": config or {}, "system": system, "tools": tools or []}


def test_diff_headers_equal_returns_empty():
    a = _hdr({"mode": "default", "model": "m"}, system="s", tools=[{"name": "t"}])
    b = _hdr({"mode": "default", "model": "m"}, system="s", tools=[{"name": "t"}])
    assert diff_headers(a, b) == {}


def test_diff_headers_reports_config_system_tools():
    a = _hdr({"mode": "default", "model": "m"}, system="s1", tools=[{"name": "t"}])
    b = _hdr({"mode": "plan", "model": "m"}, system="s2", tools=[{"name": "t"}, {"name": "u"}])
    d = diff_headers(a, b)
    assert d["config"] == {"mode": {"old": "default", "new": "plan"}}
    assert d["system_changed"] is True
    assert d["tools_changed"] is True


def test_diff_headers_prev_none_reports_all():
    b = _hdr({"mode": "plan"}, system="s", tools=[{"name": "t"}])
    d = diff_headers(None, b)
    assert d["config"] == {"mode": {"old": None, "new": "plan"}}
    assert d["system_changed"] is True
    assert d["tools_changed"] is True


def test_turn_changes_summarizes_deltas():
    log = SessionLog.create(make_header())
    log.append("turn/start", {"turn": 1})
    log.append("request/header", {"header": _hdr({"mode": "default"}), "reason": "initial"})
    log.append("user/message", _user("hi"), surface_op=APPEND)
    log.append("user/message", _user("[mem]", source="memory"), surface_op=APPEND)
    log.append("turn/end", {"turn": 1, "reason": {"kind": "completed"}})
    log.append("turn/start", {"turn": 2})
    log.append("request/header", {"header": _hdr({"mode": "plan"}, system="s2"), "reason": "change"})
    log.append("user/message", _user("hi2"), surface_op=APPEND)
    log.append("user/message", _user("[skill]", source="skill"), surface_op=APPEND)
    log.append("turn/end", {"turn": 2, "reason": {"kind": "completed"}})

    changes = log.turn_changes()
    assert changes[0] == {"turn": 1, "header_reason": "initial", "injections": ["memory"]}
    assert changes[1]["header_reason"] == "change"
    assert changes[1]["header_diff"] == {
        "config": {"mode": {"old": "default", "new": "plan"}},
        "system_changed": True,
    }
    assert changes[1]["injections"] == ["skill"]
