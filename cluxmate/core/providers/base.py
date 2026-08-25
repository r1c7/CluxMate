"""LLM provider protocol and unified types."""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]
    # Set when the provider could not parse the model's raw argument JSON.
    # The agent loop turns this into an error tool-result instead of executing,
    # so a malformed call prompts a retry rather than crashing the turn.
    parse_error: str | None = None


def parse_tool_arguments(raw: str) -> tuple[dict[str, Any], str | None]:
    """Parse OpenAI-style tool-call argument JSON defensively.

    Models occasionally emit malformed JSON — most commonly unescaped quotes or
    newlines inside a string value. Rather than letting json.loads crash the
    whole turn, return an empty input plus an error string; the agent loop feeds
    that back to the model as a tool error so it can re-issue a valid call.
    """
    if not raw or not raw.strip():
        return {}, None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        return {}, str(e)
    if not isinstance(parsed, dict):
        return {}, f"tool arguments must be a JSON object, got {type(parsed).__name__}"
    return parsed, None


# ── user-facing provider errors ─────────────────────────────────────────────
#
# Two buckets, decided at the provider layer by what the SDK threw:
#   1. LLMProviderError — the API completed (or streamed) a response whose
#      body carries a provider error.  Show THAT message verbatim to the user.
#      Covers quota, billing, rate-limit, auth failures, model-not-found,
#      server errors, and the DashScope / Qwen SSE-in-frame error path.
#   2. LLMNetworkError — pure transport failure (TCP, DNS, SSL, timeout)
#      where the call never reached the API at all.  The agent loop shows the
#      generic "网络异常，请稍后重试" fallback because there IS no provider
#      message worth displaying.

class LLMProviderError(Exception):
    """An API call reached the provider and got back a documented error body.

    ``provider_message`` is the clean human-readable text extracted from the
    body (via ``_extract_provider_message``).  It should be shown to the user
    verbatim — it IS the information they need.
    """

    def __init__(self, provider_message: str = ""):
        super().__init__(provider_message or type(self).__name__)
        self.provider_message = provider_message


class LLMNetworkError(LLMProviderError):
    """Transport-level failure: TCP, DNS, SSL, request timeout.  No provider
    message exists — the call never reached the API."""


def _extract_provider_message(body: object | None) -> str:
    """Return clean human-readable text from an SDK error body.

    The openai SDK unwraps ``{"error": {...}}`` into the inner dict. We try
    common message field names before falling back to string coercion.
    """
    if body is None:
        return ""
    if not isinstance(body, dict):
        return str(body)
    inner = body.get("error", body)
    if isinstance(inner, dict):
        for key in ("message", "msg", "status_msg"):
            val = inner.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return str(body)


@dataclass
class LLMResponse:
    text: str | None = None
    tool_calls: list[ToolCall] | None = None
    stop_reason: str = "end_turn"  # "end_turn" | "tool_use" | "max_tokens"
    # Token usage reported by the provider for THIS call, when available. The
    # agent loop uses input_tokens as the primary (exact) signal for context
    # budgeting; None when the provider/proxy omits usage.
    input_tokens: int | None = None
    output_tokens: int | None = None
    # Prompt-cache token counts for THIS call. OpenAI puts them under
    # usage.prompt_tokens_details. Unified here so the agent loop (and UI) can
    # compute per-turn cache hit rates without knowing which provider served
    # the response. None when unavailable.
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    # Reasoning/thinking content from models that emit it (DeepSeek R1, etc.).
    # Captured during streaming and NOT mixed into `text` — the UI renders it
    # separately in a collapsible panel.
    reasoning: str | None = None


@dataclass
class AssistantMessage:
    """Represents the assistant's turn in the message history."""
    text: str | None = None
    tool_calls: list[ToolCall] | None = None

    def to_api_format(self) -> dict[str, Any]:
        raise NotImplementedError


@dataclass
class ToolResultMessage:
    """A tool result to append to message history."""
    tool_call_id: str
    content: str
    name: str | None = None  # some APIs require the tool name in results

    def to_api_format(self) -> dict[str, Any]:
        raise NotImplementedError


class LLMProvider(Protocol):
    """Protocol for LLM API providers."""

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        on_thinking: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        """Send messages + tools to the model, return unified response."""
        ...

    def assistant_message_to_api(
        self, msg: AssistantMessage
    ) -> dict[str, Any]:
        """Convert an AssistantMessage to provider-specific API format."""
        ...

    def tool_result_to_api(
        self, result: ToolResultMessage
    ) -> dict[str, Any]:
        """Convert a ToolResultMessage to provider-specific API format."""
        ...

    def max_tokens(self) -> int:
        """Default max tokens for this provider."""
        ...

    def reset(self) -> Any:
        """Rebuild any event-loop-bound state (e.g. async HTTP client).

        Called at the start of each turn so a provider reused across turns
        rebinds to the current event loop instead of a closed one. Returns the
        newly-created client handle, which the caller must pass back to
        ``aclose`` at turn end so overlapping turns each close only their own
        client rather than the shared provider's current one.
        """
        ...

    async def aclose(self, client: Any | None = None) -> None:
        """Close an async HTTP client on the CURRENT (still-open) event loop.

        Closes ``client`` (default: the provider's current client). Called at the
        end of each turn, before the turn's loop is closed, with the handle
        ``reset()`` returned. Without this the client's connection pool is torn
        down later by the GC finalizer on an already-closed loop, raising 'Event
        loop is closed'.
        """
        ...
