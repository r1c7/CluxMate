"""The trust state must reach BOTH the model (injection) and the audit trail."""

import json
from pathlib import Path

from cluxmate.core.builder import AgentBuilder
from cluxmate.core.session_log import SessionHeader, SessionLog
from cluxmate.core.trust import resolve_trust


class _Provider:
    pass


def _builder(tmp_path, monkeypatch, trusted: bool | None = None):
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    cwd = tmp_path / "proj"
    state = cwd / ".cluxmate"
    state.mkdir(parents=True)
    (state / "settings.json").write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    store_path = tmp_path / "trust.json"
    from cluxmate.core.trust import TrustStore

    store = TrustStore(store_path)
    if trusted is not None:
        store.set(str(cwd), "trusted" if trusted else "denied")
    b = AgentBuilder(str(cwd), _Provider()).with_default_tools()
    b.with_trust(resolve_trust(str(cwd), store))
    return b


def test_untrusted_first_turn_injects_a_trust_note(tmp_path, monkeypatch):
    b = _builder(tmp_path, monkeypatch)
    injections = b.injections_for_turn()
    assert [src for src, _ in injections] == ["trust"]
    body = injections[0][1]
    assert "[Project trust]" in body
    assert "settings.json" in body
    # Fingerprint de-duplication: the note is not re-sent on the next turn.
    assert b.injections_for_turn() == []


def test_trusted_directory_injects_nothing(tmp_path, monkeypatch):
    b = _builder(tmp_path, monkeypatch, trusted=True)
    assert b.injections_for_turn() == []


def test_untrusted_without_project_config_injects_nothing(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    cwd = tmp_path / "plain"
    cwd.mkdir()
    b = AgentBuilder(str(cwd), _Provider()).with_default_tools()
    b.with_trust(resolve_trust(str(cwd), None))
    assert b.injections_for_turn() == []


def test_build_carries_the_trust_state_into_the_agent(tmp_path, monkeypatch):
    b = _builder(tmp_path, monkeypatch)
    log = SessionLog.create(SessionHeader(id="s1", createdAt=0))
    agent = b.build(session_log=log)
    assert agent.trusted is False
    assert agent.trust_source == "default"
    assert b.build_child is not None  # child builders inherit _trust (see _child_builder)


def test_child_builder_inherits_the_trust_state(tmp_path, monkeypatch):
    b = _builder(tmp_path, monkeypatch)
    b.with_subagents()
    child = b.build_child("explore", "look around")
    assert child.trusted is False
