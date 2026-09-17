"""The JSON-RPC side of the turn-usage contract (AgentCallbacks.on_usage).

The desktop's session token footer sums, per reply, the reply's prompt and
completion tokens. A turn that is interrupted or aborted answers with
``{stop_reason, text, history}`` and no usage at all, so the only way those
numbers reach the UI is the live ``usage`` stream event emitted from INSIDE the
turn. It must therefore:
  * carry the whole turn_usage payload the agent publishes, and
  * be emitted by the ROOT callbacks only — a subagent's totals ride on
    agent_end, and emitting them here too would double-count them.
"""

import pytest

from cluxmate.core import jsonrpc_server
from cluxmate.core.jsonrpc_server import JsonRpcCallbacks


class _Emitter:
    """Stands in for the stdout writer, recording the events emitted."""

    def __init__(self, monkeypatch):
        self.events: list[dict] = []
        monkeypatch.setattr(jsonrpc_server, "_write_dict", self)

    def __call__(self, payload: dict):
        self.events.append(payload)

    def of_type(self, kind: str) -> list[dict]:
        return [
            e["params"] for e in self.events
            if (e.get("params") or {}).get("type") == kind
        ]


@pytest.mark.asyncio
async def test_on_usage_emits_the_full_running_total(monkeypatch):
    cbs = JsonRpcCallbacks(None)
    emitter = _Emitter(monkeypatch)

    await cbs.on_usage({
        "input_tokens": 1200,
        "output_tokens": 34,
        "cache_read": 1000,
        "cache_write": 0,
        "ttft_ms": 512,
        "gen_ms": 4096,
        "turn": 3,
        "step": 2,
        "time": 1789000000000,
    })

    usage = emitter.of_type("usage")
    assert len(usage) == 1
    assert usage[0] == {
        "type": "usage",
        "agent_id": "root",
        "input_tokens": 1200,
        "output_tokens": 34,
        "cache_read": 1000,
        "cache_write": 0,
        "ttft_ms": 512,
        "gen_ms": 4096,
        # Attribution fields ride through untouched: the renderer needs them to
        # drop a superseded turn's late event.
        "turn": 3,
        "step": 2,
        "time": 1789000000000,
    }
    # It must ride the chat/stream channel every front-end already drains.
    assert emitter.events[0]["method"] == "chat/stream"


@pytest.mark.asyncio
async def test_scoped_subagent_callbacks_emit_no_usage_event(monkeypatch):
    """A child reports tokens through agent_end; a per-call usage event from the
    child would be attributed to the root reply and double-count them."""
    shared = JsonRpcCallbacks(None)
    emitter = _Emitter(monkeypatch)

    child = shared.scoped("child-1")
    await child.on_usage({"input_tokens": 7, "output_tokens": 7})

    assert emitter.of_type("usage") == []
