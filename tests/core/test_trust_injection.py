"""The trust state must reach BOTH the model (injection) and the audit trail."""

import json
from pathlib import Path

import pytest

from cluxmate.core.builder import AgentBuilder
from cluxmate.core.providers.base import LLMResponse
from cluxmate.core.retrieval_memory import RetrievalConfig
from cluxmate.core.session_log import SessionHeader, SessionLog
from cluxmate.core.trust import resolve_trust


class _Provider:
    """Minimal provider: ``build()`` reads ``max_tokens()``, ``run()`` chats once.

    Kept to the shape of the RecordingProvider in ``tests/core/test_agent_logging.py``
    so a logged turn here behaves like a logged turn anywhere else.
    """

    def __init__(self, text: str = "ok") -> None:
        self._text = text

    def max_tokens(self) -> int:
        return 1000

    async def chat(self, messages, tools, *, on_delta=None, on_thinking=None):
        if on_delta is not None:
            await on_delta(self._text)
        return LLMResponse(text=self._text, stop_reason="end_turn")

    def assistant_message_to_api(self, msg) -> dict:
        return {"role": "assistant", "content": msg.text or ""}

    def tool_result_to_api(self, result) -> dict:
        return {
            "role": "tool",
            "tool_call_id": result.tool_call_id,
            "content": result.content,
        }


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


def _retrieval_config(tmp_path) -> RetrievalConfig:
    p = tmp_path / "retrieval-memory.json"
    p.write_text(json.dumps({"enabled": True}), encoding="utf-8")
    return RetrievalConfig(p)


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


def test_untrusted_builder_hands_the_decision_to_every_reader_it_builds(
    tmp_path, monkeypatch
):
    """The gate is per-reader, so the wiring itself needs an assertion.

    Injection tests cannot see this: the untrusted fixture ships no skills /
    subagents / LSP config, so a reader built with the default ``trusted=True``
    would still render nothing and stay green.
    """
    b = _builder(tmp_path, monkeypatch)
    b.with_retrieval_memory(_retrieval_config(tmp_path))

    assert b._hooks_manager()._trusted is False
    assert b._agent_registry()._trusted is False
    assert b._retrieval_manager()._trusted is False
    assert b._lsp_manager()._trusted is False


def test_untrusted_builder_hands_the_decision_to_the_deferred_mcp_manager(
    tmp_path, monkeypatch
):
    """MCPManager is constructed on two paths, both gated by the same flag."""
    b = _builder(tmp_path, monkeypatch).with_deferred_mcp()
    b._get_tools()  # constructs the manager without spawning it
    assert b._mcp is not None and b._mcp._trusted is False


def test_untrusted_builder_hands_the_decision_to_the_loaded_mcp_manager(
    tmp_path, monkeypatch
):
    b = _builder(tmp_path, monkeypatch).with_deferred_mcp()
    assert b.load_mcp() is False  # nothing configured → constructs, spawns nothing
    assert b._mcp is not None and b._mcp._trusted is False


def test_build_carries_the_trust_state_into_the_agent(tmp_path, monkeypatch):
    b = _builder(tmp_path, monkeypatch)
    log = SessionLog.create(SessionHeader(id="s1", createdAt=0))
    agent = b.build(session_log=log)
    assert agent.trusted is False
    assert agent.trust_source == "default"


@pytest.mark.asyncio
async def test_logged_turn_records_an_untrusted_decision_in_the_header(
    tmp_path, monkeypatch
):
    b = _builder(tmp_path, monkeypatch)
    log = SessionLog.create(SessionHeader(id="s1", createdAt=0, apiType="openai"))
    agent = b.build(session_log=log)
    await agent.run("hi")

    header = next(e for e in log.events if e.type == "request/header").data["header"]
    assert header["config"]["trusted"] is False
    assert header["config"]["trust_source"] == "default"


@pytest.mark.asyncio
async def test_logged_turn_records_a_trusted_decision_in_the_header(
    tmp_path, monkeypatch
):
    b = _builder(tmp_path, monkeypatch, trusted=True)
    log = SessionLog.create(SessionHeader(id="s2", createdAt=0, apiType="openai"))
    agent = b.build(session_log=log)
    await agent.run("hi")

    header = next(e for e in log.events if e.type == "request/header").data["header"]
    # Contrast case: a stored decision is not the same value as the default, so a
    # hard-coded "trusted is False" cannot pass both tests.
    assert header["config"]["trusted"] is True
    assert header["config"]["trust_source"] == "registry"


def test_child_builder_inherits_the_trust_state(tmp_path, monkeypatch):
    b = _builder(tmp_path, monkeypatch)
    b.with_subagents()
    child = b.build_child("explore", "look around")
    assert child.trusted is False
