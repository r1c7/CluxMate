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


def _app_module():
    pytest.importorskip("textual")
    from cluxmate.tui import app as app_mod

    return app_mod


def _app_class():
    return _app_module().CluxMateApp


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

    def __init__(self, cwd, llm_provider, project_root=None):
        self.cwd = cwd
        self.project_root = project_root
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


class _StubMCP:
    """Records the trust flag + config root the controller passes to MCPManager.

    The real manager reads ``~/.cluxmate/mcp.json`` and spawns its servers, so
    a test must never let one near the developer's home directory.
    """

    def __init__(self, cwd, *, trusted=True, config_root=None):
        self.cwd = cwd
        self.config_root = config_root
        self._trusted = trusted

    def load(self):
        return {}


class _StubProvider:
    effort = "unset"

    def set_reasoning_effort(self, effort):
        self.effort = effort


def _built_controller(tmp_path, monkeypatch) -> TuiController:
    _home(tmp_path, monkeypatch)
    _StubBuilder.built = []
    monkeypatch.setattr(controller_mod, "AgentBuilder", _StubBuilder)
    monkeypatch.setattr(controller_mod, "MCPManager", _StubMCP)
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
    _home(tmp_path, monkeypatch)
    monkeypatch.setattr(controller_mod, "MCPManager", _StubMCP)
    cwd = _project(tmp_path)
    ctrl = TuiController()

    untrusted = ctrl._ensure_mcp(str(cwd), False)
    trusted = ctrl._ensure_mcp(str(cwd), True)
    # The stub proves the real manager (which reads ~/.cluxmate/mcp.json and
    # spawns its servers) is never constructed here.
    assert isinstance(untrusted, _StubMCP)
    assert untrusted is not trusted
    assert untrusted._trusted is False
    assert trusted._trusted is True
    assert ctrl._ensure_mcp(str(cwd), False) is untrusted
    assert ctrl._ensure_mcp(str(cwd), True) is trusted


# ── the real app, mounted on a directory the gate must ask about ───────────


def _gate_app(tmp_path, monkeypatch):
    """A real ``CluxMateApp`` whose working directory ships project config.

    Session creation (what builds the agent) is recorded instead of run, so the
    test observes the gate without starting MCP servers or hooks.
    """
    app_mod = _app_module()
    home = _home(tmp_path, monkeypatch)
    cwd = _project(tmp_path)
    app = app_mod.CluxMateApp()
    app._cwd = str(cwd)
    entry = {
        "id": "m1", "api_key": "k", "model_name": "the-model",
        "provider": "openai", "api_type": "openai",
    }
    # on_mount only asks "is there a key" before reaching the gate.
    monkeypatch.setattr(app.ctrl.config, "list_models", lambda: [entry])
    monkeypatch.setattr(app.ctrl.config, "get_model", lambda model_id: entry)
    monkeypatch.setattr(app.ctrl.config, "get_active_model_id", lambda: "m1")
    created: list[str] = []

    def _record_new_session(model_id, session_cwd, mode="default"):
        created.append(session_cwd)
        return "sid-new"

    monkeypatch.setattr(app.ctrl, "new_session", _record_new_session)
    return app, cwd, home, created


def _chat_lines(app):
    return [strip.text for strip in app.query_one("#chat-log").lines]


@pytest.mark.asyncio
async def test_session_actions_defer_to_the_pending_trust_question(tmp_path, monkeypatch):
    """While the gate waits for an answer, a session action must neither create
    a session (agent/MCP/hooks) nor clear the chat the question lives in."""
    app, cwd, home, created = _gate_app(tmp_path, monkeypatch)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._question_future is not None, "the gate should be waiting"
        assert any("Trust" in line for line in _chat_lines(app))

        # Every route that creates or loads a session, while the question waits.
        app.action_new_session()
        await pilot.click("#btn-new-session")
        app._load_session("some-other-session")
        await pilot.pause()

        # Observed while the question is still pending. Answering it afterwards
        # lets the app shut down cleanly, so a failure here is reported as one.
        created_while_pending = list(created)
        question_still_shown = any("Trust" in line for line in _chat_lines(app))
        still_pending = app._question_future is not None
        told_why = "Answer the pending question first" in "\n".join(
            _chat_lines(app)
        )

        app.query_one("#prompt-input").focus()
        await pilot.press("2")
        await pilot.press("enter")
        await pilot.pause()

        assert created_while_pending == [], "a session was created before the answer"
        assert still_pending, "the question stopped waiting for an answer"
        assert question_still_shown, "the question was wiped out of the chat"
        assert told_why, "a blocked session action gave no hint"

        # ...and it was still answerable: the answer reached the gate, which then
        # created the session it had withheld.
        assert app._question_future is None
        assert created == [str(cwd)]
        decision = app.ctrl.trust_for(str(cwd))
        assert decision.trusted is True
        assert decision.source == "session"
        assert not (home / ".cluxmate" / "trust.json").exists()


@pytest.mark.asyncio
async def test_declining_trust_names_the_escape_hatch_in_the_same_run(tmp_path, monkeypatch):
    """Declining has to say, right there and once, what is not loaded and how to
    load it — waiting for the next launch leaves the user without a way out."""
    app, cwd, home, created = _gate_app(tmp_path, monkeypatch)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt-input").focus()
        await pilot.press("3")
        await pilot.press("enter")
        await pilot.pause()

        assert created == [str(cwd)]
        assert app.ctrl.trust_for(str(cwd)).status == DENIED
        # Remembered, so the next launch does not ask again.
        assert '"denied"' in (
            home / ".cluxmate" / "trust.json"
        ).read_text(encoding="utf-8")

        text = "\n".join(_chat_lines(app))
        assert text.count("Project config not loaded") == 1, text
        notice = text.split("Project config not loaded", 1)[1]
        assert "hooks" in notice
        assert "cluxmate trust add" in notice

