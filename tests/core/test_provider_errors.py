"""Provider error fallback — SDK errors become unified exceptions, never raw.

Two-bucket guarantee under test:
1. Transport failure (TCP/DNS/SSL, timeout) → LLMNetworkError → generic
   "网络异常，请稍后重试" (the call never reached the API, so there is no
   provider message worth showing).
2. API response carrying an error body (quota, billing, auth, rate-limit,
   model-not-found, server error, DashScope SSE-in-frame) → LLMProviderError
   with cleaned provider text → shown verbatim.
"""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from cluxmate.core.providers.base import (
    LLMNetworkError,
    LLMProviderError,
    _extract_provider_message,
)
from cluxmate.core.providers.factory import build_provider


def _entry(**over):
    base = {
        "id": "x", "api_type": "openai", "provider": "P", "base_url": "",
        "api_key": "k", "model_name": "m", "context_1m": False,
    }
    base.update(over)
    return base


def _http_response(status: int) -> httpx.Response:
    return httpx.Response(
        status, request=httpx.Request("POST", "https://api.test")
    )


# ── provider message extraction ────────────────────────────────────────────

def test_extract_openai_style_envelope():
    body = {"error": {"message": "Insufficient balance.", "code": "insufficient_quota"}}
    assert _extract_provider_message(body) == "Insufficient balance."


def test_extract_already_unwrapped_body():
    body = {"code": "1113", "message": "余额不足或无可用资源包，请充值。"}
    assert _extract_provider_message(body) == "余额不足或无可用资源包，请充值。"


def test_extract_falls_back():
    assert _extract_provider_message(None) == ""
    assert _extract_provider_message("plain text") == "plain text"
    assert "余额不足" in _extract_provider_message({"error": "余额不足"})


# ── OpenAI-compatible provider translation ─────────────────────────────────

def _openai_status_error(status: int, message: str):
    from openai import APIStatusError
    return APIStatusError(
        message,
        response=_http_response(status),
        body={"error": {"message": message}},
    )


def _openai_client_raising(err: Exception):
    async def create(**kwargs):
        raise err
    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )


@pytest.mark.asyncio
async def test_openai_quota_error_surfaces_provider_message():
    """402 insufficient balance (DeepSeek) → LLMProviderError with clean text."""
    p = build_provider(_entry())
    p._client = _openai_client_raising(
        _openai_status_error(402, "Access denied, please check your balance.")
    )
    with pytest.raises(LLMProviderError) as ei:
        await p.chat([{"role": "user", "content": "hi"}], [])
    assert ei.value.provider_message == "Access denied, please check your balance."


@pytest.mark.asyncio
async def test_openai_auth_error_surfaces_provider_message():
    """401 → LLMProviderError (not network — the API DID respond)."""
    p = build_provider(_entry())
    p._client = _openai_client_raising(_openai_status_error(401, "Invalid API key"))
    with pytest.raises(LLMProviderError) as ei:
        await p.chat([{"role": "user", "content": "hi"}], [])
    assert "Invalid API key" in ei.value.provider_message


@pytest.mark.asyncio
async def test_openai_rate_limit_surfaces_provider_message():
    p = build_provider(_entry())
    p._client = _openai_client_raising(
        _openai_status_error(429, "Rate limit reached for model 'x'")
    )
    with pytest.raises(LLMProviderError):
        await p.chat([{"role": "user", "content": "hi"}], [])


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 500, 503])
async def test_openai_all_status_errors_become_llm_provider_error(status):
    """All HTTP error responses → LLMProviderError.  The provider DID talk back."""
    p = build_provider(_entry())
    p._client = _openai_client_raising(_openai_status_error(status, "err"))
    with pytest.raises(LLMProviderError):
        await p.chat([{"role": "user", "content": "hi"}], [])


@pytest.mark.asyncio
async def test_openai_sse_path_too_became_provider_error():
    """Streaming APIError (DashScope SSE frame) → LLMProviderError."""
    p = build_provider(_entry())
    p._client = _openai_client_raising(_openai_status_error(404, "Model not found"))

    async def on_delta(chunk):
        pass

    with pytest.raises(LLMProviderError):
        await p.chat(
            [{"role": "user", "content": "hi"}], [], on_delta=on_delta
        )


@pytest.mark.asyncio
async def test_openai_timeout_maps_to_asyncio_timeout():
    """Timeout → asyncio.TimeoutError (agent loop maps this to network fallback)."""
    from openai import APITimeoutError

    p = build_provider(_entry())
    p._client = _openai_client_raising(APITimeoutError(request=None))
    with pytest.raises(asyncio.TimeoutError):
        await p.chat([{"role": "user", "content": "hi"}], [])


@pytest.mark.asyncio
async def test_openai_connection_error_becomes_llm_network_error():
    """APIConnectionError → LLMNetworkError (TCP never reached the API)."""
    from openai import APIConnectionError

    p = build_provider(_entry())
    p._client = _openai_client_raising(APIConnectionError(request=None))
    with pytest.raises(LLMNetworkError):
        await p.chat([{"role": "user", "content": "hi"}], [])


@pytest.mark.asyncio
async def test_openai_read_error_becomes_llm_network_error():
    """httpx.ReadError → LLMNetworkError, never a raw error.

    A connection torn down mid-body (anyio.ClosedResourceError under the hood)
    surfaces as a bare httpx.ReadError while iterating the SSE stream — the SDK
    only wraps the initial request as APIConnectionError, so the provider must
    catch ReadError directly instead of letting it escape the unified buckets.
    """
    p = build_provider(_entry())
    p._client = _openai_client_raising(
        httpx.ReadError(
            "connection closed",
            request=httpx.Request("POST", "https://api.test"),
        )
    )
    with pytest.raises(LLMNetworkError):
        await p.chat([{"role": "user", "content": "hi"}], [])
