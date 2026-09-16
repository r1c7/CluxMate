"""The TUI controller must resolve and forward the trust decision."""

from pathlib import Path

import pytest

from cluxmate.core.trust import DENIED, TRUSTED
from cluxmate.tui import controller as controller_mod
from cluxmate.tui.controller import TuiController


def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    return home


def _project(tmp_path) -> Path:
    cwd = tmp_path / "proj"
    (cwd / ".cluxmate").mkdir(parents=True)
    (cwd / ".cluxmate" / "settings.json").write_text("{}", encoding="utf-8")
    return cwd


def test_trust_for_reports_the_projects_findings(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    cwd = _project(tmp_path)
    ctrl = TuiController()
    decision = ctrl.trust_for(str(cwd))
    assert decision.pending is True
    assert [f.kind for f in decision.findings] == ["hooks"]


def test_set_session_trust_flips_the_decision_without_writing_the_registry(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    cwd = _project(tmp_path)
    ctrl = TuiController()
    ctrl.set_session_trust(str(cwd), TRUSTED)
    assert ctrl.trust_for(str(cwd)).trusted is True
    assert not (home / ".cluxmate" / "trust.json").exists()


# ── the app's answer → (status, session_only) mapping ──────────────────────
# Pure, so it is testable without a Textual app (there is no TUI test harness).


def _app_class():
    pytest.importorskip("textual")
    from cluxmate.tui.app import CluxMateApp

    return CluxMateApp


def test_every_offered_option_has_an_answer_mapping():
    app = _app_class()
    labels = [o["label"] for o in app.TRUST_OPTIONS]
    assert sorted(labels) == sorted(app.TRUST_ANSWERS)


def test_trust_answer_for_maps_the_three_options():
    app = _app_class()
    assert app._trust_answer_for(["Trust and remember"]) == (TRUSTED, False)
    assert app._trust_answer_for(["Trust this run only"]) == (TRUSTED, True)
    assert app._trust_answer_for(["Do not trust"]) == (DENIED, False)


def test_trust_answer_for_ignores_unrecognized_labels():
    app = _app_class()
    assert app._trust_answer_for([]) is None
    assert app._trust_answer_for(["yes"]) is None
    # The number-only answer path cannot reach it, but a stale label must not
    # silently grant trust either.
    assert app._trust_answer_for(["Trust and remember", "yes"]) == (TRUSTED, False)


# ── the decision reaches the build ─────────────────────────────────────────


class _StubBuilder:
    """Records what `_build_agent` hands the real AgentBuilder."""

    built: list = []

    def __init__(self, cwd, llm_provider):
        self.cwd = cwd
        self.trust = None
        self.mcp = None
        _StubBuilder.built.append(self)

    def with_trust(self, decision):
        self.trust = decision
        return self

    def with_default_tools(self):
        return self

    def with_subagents(self):
        return self

    def with_mode(self, mode):
        self.mode = mode
        return self

    def with_model(self, model_name):
        return self

    def with_context_1m(self, flag):
        return self

    def with_mcp(self, mcp):
        self.mcp = mcp
        return self

    def with_log_store(self, store):
        return self

    def build(self, session_log=None):
        return object()


class _StubProvider:
    effort = "unset"

    def set_reasoning_effort(self, effort):
        self.effort = effort


def _built_controller(tmp_path, monkeypatch) -> TuiController:
    _home(tmp_path, monkeypatch)
    _StubBuilder.built = []
    monkeypatch.setattr(controller_mod, "AgentBuilder", _StubBuilder)
    monkeypatch.setattr(controller_mod, "_create_provider", lambda entry: _StubProvider())
    ctrl = TuiController()
    monkeypatch.setattr(ctrl.config, "get_model", lambda model_id: {
        "id": "m1", "api_key": "k", "model_name": "the-model",
        "provider": "openai", "api_type": "openai",
    })
    return ctrl


def test_build_key_and_builder_carry_the_trust_decision(tmp_path, monkeypatch):
    cwd = _project(tmp_path)
    ctrl = _built_controller(tmp_path, monkeypatch)

    ctrl.new_session("m1", str(cwd), "default")
    assert ctrl._build_key == (str(cwd), "the-model", "default", "m1", False)
    assert _StubBuilder.built[-1].trust.trusted is False
    assert _StubBuilder.built[-1].mcp is ctrl._ensure_mcp(str(cwd), False)

    # A same-decision build is reused (the cheap path stays cheap)...
    count = len(_StubBuilder.built)
    ctrl._build_agent("m1", str(cwd), "default")
    assert len(_StubBuilder.built) == count

    # ...but a trusted decision must never reuse the untrusted agent.
    ctrl.set_session_trust(str(cwd), TRUSTED)
    ctrl._build_agent("m1", str(cwd), "default")
    assert ctrl._build_key == (str(cwd), "the-model", "default", "m1", True)
    assert len(_StubBuilder.built) == count + 1
    assert _StubBuilder.built[-1].trust.trusted is True


def test_mcp_manager_is_cached_and_gated_on_trust(tmp_path, monkeypatch):
    cwd = _project(tmp_path)
    ctrl = TuiController()

    untrusted = ctrl._ensure_mcp(str(cwd), False)
    trusted = ctrl._ensure_mcp(str(cwd), True)
    assert untrusted is not trusted
    assert untrusted._trusted is False
    assert trusted._trusted is True
    assert ctrl._ensure_mcp(str(cwd), False) is untrusted
    assert ctrl._ensure_mcp(str(cwd), True) is trusted
