"""Tests for the provider factory — api_type routing, max_tokens, model wiring."""

import pytest

from cluxmate.core.providers.factory import build_provider
from cluxmate.core.providers.openai import OpenAIProvider


def _entry(**over):
    base = {
        "id": "x", "api_type": "openai", "provider": "P", "base_url": "",
        "api_key": "k", "model_name": "m", "context_1m": False,
    }
    base.update(over)
    return base


def test_openai_entry_builds_openai_provider():
    p = build_provider(_entry(api_type="openai", model_name="gpt-5.1",
                              base_url="https://api.openai.com/v1"))
    assert isinstance(p, OpenAIProvider)
    # Regression guard for the model-is-a-no-op bug: the provider must actually
    # carry the entry's model_name so chat() sends it.
    assert p._model == "gpt-5.1"
    # max_tokens defaults to 32768: thinking models charge reasoning against
    # the output budget, and a smaller cap let long reasoning eat everything
    # and truncate the turn before any reply (the silent "(no output)" bug).
    assert p.max_tokens() == 32768


def test_deepseek_base_url_uses_32768_default():
    p = build_provider(_entry(api_type="openai", model_name="deepseek-v4-flash",
                              base_url="https://api.deepseek.com"))
    assert isinstance(p, OpenAIProvider)
    assert p._model == "deepseek-v4-flash"
    assert p.max_tokens() == 32768


def test_empty_base_url_becomes_none():
    p = build_provider(_entry(base_url="", api_type="openai"))
    assert p._base_url is None


def test_openai_usage_maps_to_llmresponse():
    """A stubbed OpenAI-style response's usage lands in LLMResponse."""
    import asyncio
    from types import SimpleNamespace

    p = build_provider(_entry(api_type="openai"))

    async def fake_create(**kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="hi", tool_calls=None),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(prompt_tokens=123, completion_tokens=45),
        )

    p._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
    resp = asyncio.run(p.chat([{"role": "user", "content": "hi"}], []))
    assert resp.input_tokens == 123
    assert resp.output_tokens == 45


def test_openai_assistant_message_content_never_null():
    """DeepSeek 400s ('content field is a required field') on content: null,
    so a text-less assistant turn must serialize with '' instead of None."""
    from cluxmate.core.providers.base import AssistantMessage, ToolCall

    p = build_provider(_entry(api_type="openai"))
    msg = AssistantMessage(
        tool_calls=[ToolCall(id="c1", name="bash", input={"command": "ls"})]
    )
    out = p.assistant_message_to_api(msg)
    assert out["content"] == ""
    assert out["content"] is not None
    assert out["tool_calls"][0]["function"]["name"] == "bash"

    # Plain-text turn keeps its text.
    out2 = p.assistant_message_to_api(AssistantMessage(text="hello"))
    assert out2["content"] == "hello"


def test_openai_chat_repairs_null_content_in_replayed_history():
    """Sessions saved before the null-content fix replay history entries
    with content: null; the provider must repair them at the boundary."""
    import asyncio
    from types import SimpleNamespace

    p = build_provider(_entry(api_type="openai"))
    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="ok", tool_calls=None),
                finish_reason="stop",
            )],
            usage=None,
        )

    p._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "bash", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "done"},
    ]
    asyncio.run(p.chat(history, []))
    sent = captured["messages"]
    assert sent[1]["content"] == ""
    assert sent[1]["tool_calls"] == history[1]["tool_calls"]
    # The repair must copy, not mutate the caller's history in place.
    assert history[1]["content"] is None


def test_openai_tool_result_content_never_null():
    from cluxmate.core.providers.base import ToolResultMessage

    p = build_provider(_entry(api_type="openai"))
    out = p.tool_result_to_api(
        ToolResultMessage(tool_call_id="c1", content="", name="bash")
    )
    assert out["content"] == ""
    assert out["content"] is not None


def _timeout_stub_client():
    """A stub client whose create() always raises the SDK's APITimeoutError."""
    from types import SimpleNamespace
    from openai import APITimeoutError

    calls = {"n": 0}

    async def fake_create(**kwargs):
        calls["n"] += 1
        raise APITimeoutError(request=None)

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
    )
    return client, calls


def test_openai_streaming_timeout_maps_to_asyncio_timeout():
    """openai.APITimeoutError SUBCLASSES APIConnectionError. The timeout
    handler must run first and map it to asyncio.TimeoutError (which the
    agent loop turns into a graceful '[LLM request timed out]' message) —
    not the connection-error retry, which re-raises the raw SDK error and
    crashes the turn (the Qwen 'Request timed out.' desktop bug)."""
    import asyncio

    p = build_provider(_entry(api_type="openai"))
    client, calls = _timeout_stub_client()
    p._client = client

    async def on_delta(chunk):
        pass

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(
            p.chat([{"role": "user", "content": "hi"}], [], on_delta=on_delta)
        )
    # A read timeout means the server already had the full read window —
    # retrying would only double a multi-minute wait.
    assert calls["n"] == 1


def test_openai_blocking_timeout_maps_to_asyncio_timeout():
    import asyncio

    p = build_provider(_entry(api_type="openai"))
    client, calls = _timeout_stub_client()
    p._client = client

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(p.chat([{"role": "user", "content": "hi"}], []))
    assert calls["n"] == 1


def test_openai_connection_error_still_retries_once():
    """Genuine connection errors (TCP reset etc. — not timeouts) keep their
    single retry before surfacing — as the unified LLMNetworkError, never the
    raw SDK exception (the agent loop turns that into the friendly fallback)."""
    import asyncio
    from types import SimpleNamespace
    from openai import APIConnectionError

    from cluxmate.core.providers.base import LLMNetworkError

    p = build_provider(_entry(api_type="openai"))
    calls = {"n": 0}

    async def fake_create(**kwargs):
        calls["n"] += 1
        raise APIConnectionError(request=None)

    p._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
    )
    with pytest.raises(LLMNetworkError):
        asyncio.run(p.chat([{"role": "user", "content": "hi"}], []))
    assert calls["n"] == 2


def test_openai_client_timeout_is_slow_first_token_friendly():
    """The httpx read window must comfortably exceed the streaming idle guard
    and allow slow-first-token endpoints (Qwen / reasoning models > 60s)."""
    import httpx

    from cluxmate.core.providers import openai as oai_mod

    p = build_provider(_entry(api_type="openai"))
    timeout = p._client.timeout
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read >= 180.0
    assert timeout.read > oai_mod.IDLE_TIMEOUT
    assert timeout.connect <= 30.0  # dead endpoints still fail fast


def test_openai_length_finish_maps_to_max_tokens():
    """finish_reason 'length' (output budget exhausted) must surface as
    stop_reason 'max_tokens', not masquerade as end_turn — thinking models
    get cut off mid-reasoning and would otherwise show a silent empty reply."""
    p = build_provider(_entry(api_type="openai"))

    r = p._assemble("", None, "length", None, None)
    assert r.stop_reason == "max_tokens"
    assert r.text == ""

    # Partial reply truncated mid-generation keeps its text AND the signal.
    r2 = p._assemble("half done", None, "length", None, None)
    assert r2.stop_reason == "max_tokens"
    assert r2.text == "half done"

    # Normal finishes are untouched.
    assert p._assemble("ok", None, "stop", None, None).stop_reason == "end_turn"
