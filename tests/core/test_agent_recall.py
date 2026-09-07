"""Tests for the per-turn recall injection hook in AgentLoop."""

import json
from pathlib import Path
from typing import Any

import pytest

from cluxmate.core.agent import AgentLoop
from cluxmate.core.providers.base import LLMResponse
from cluxmate.core.retrieval_memory import RetrievalConfig, RetrievalMemory
from cluxmate.core.session_log import SessionHeader, SessionLog
from cluxmate.tools.base import ToolBridge


class _Provider:
    def __init__(self):
        self.calls: list[tuple[list[dict], list[dict]]] = []

    async def chat(self, messages, tools, *, on_delta=None, on_thinking=None):
        self.calls.append((messages, tools))
        return LLMResponse(text="ok", stop_reason="end_turn")

    def assistant_message_to_api(self, msg) -> dict:
        return {"role": "assistant", "content": msg.text or ""}

    def tool_result_to_api(self, result) -> dict:
        return {"role": "tool", "tool_call_id": result.tool_call_id, "content": result.content}

    def max_tokens(self) -> int:
        return 1000


def _make_log(sid: str = "s1") -> SessionLog:
    return SessionLog.create(SessionHeader(id=sid, createdAt=0, apiType="openai"))


@pytest.mark.asyncio
async def test_recall_injected_after_human(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    cwd = tmp_path / "proj"
    cwd.mkdir()
    cfg = tmp_path / "retrieval-memory.json"
    cfg.write_text(json.dumps({"enabled": True}), encoding="utf-8")
    mem = RetrievalMemory(str(cwd), RetrievalConfig(cfg))
    mem.remember("Use pytest for tests.", scope="global")

    log = _make_log()
    agent = AgentLoop(
        model="test", provider=_Provider(), tools=ToolBridge(),
        system_prompt="s", session_log=log, retrieval=mem,
    )
    await agent.run("what test framework should I use?")

    sources = [e.data.get("source") for e in log.events if e.type == "user/message"]
    assert sources == ["human", "memory-recall"]
    recall = next(
        e for e in log.events
        if e.type == "user/message" and e.data.get("source") == "memory-recall"
    )
    assert "Use pytest" in recall.data["message"]["content"]
    # The recall block is in the derived surface (model-visible) AND in the
    # actual request the model received.
    assert any("Use pytest" in m.get("content", "") for m in log.derive_messages())
    assert any("Use pytest" in m.get("content", "") for m in agent.provider.calls[-1][0])


@pytest.mark.asyncio
async def test_no_retrieval_no_recall_message(tmp_path):
    log = _make_log()
    agent = AgentLoop(
        model="test", provider=_Provider(), tools=ToolBridge(),
        system_prompt="s", session_log=log,
    )
    await agent.run("hi")
    assert all(
        e.data.get("source") != "memory-recall"
        for e in log.events if e.type == "user/message"
    )
