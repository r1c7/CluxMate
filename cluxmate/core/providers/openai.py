"""OpenAI Chat Completions API provider."""

import asyncio
import json
import os
from typing import Any, Awaitable, Callable

import httpx
from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
)

from .base import (
    AssistantMessage,
    LLMNetworkError,
    LLMProvider,
    LLMProviderError,
    LLMResponse,
    ToolCall,
    ToolResultMessage,
    _extract_provider_message,
    parse_tool_arguments,
)
from ..reasoning import coerce_effort, detect_dialect, wire_for

# OpenAI finish reasons that indicate tool calls
TOOL_FINISH = {"tool_calls", "function_call"}

# Max wait between streamed chunks before we treat the stream as stalled. This
# replaces a fixed overall cap: a long reply may legitimately stream for minutes,
# but a gap this large means the connection is dead. Raised as asyncio.TimeoutError.
IDLE_TIMEOUT = 90.0

# Once finish_reason has arrived, the visible answer is COMPLETE — anything left
# on the stream is just the trailing usage-only chunk (token counts / prompt-cache
# stats, requested via stream_options.include_usage) followed by close. Some
# endpoints are slow to emit that tail or to close the SSE connection: the turn
# would otherwise stay blocked in __anext__() for up to IDLE_TIMEOUT (reset by
# keepalives, so potentially minutes) with the answer already fully rendered —
# the UI sits at "working" long after the reply is done, and the per-turn cache
# hit rate (which rides that usage chunk) only lands when the stream finally
# closes. So after finish_reason we wait only this long for the usage tail, then
# give up and return: the hit rate may be missing/partial, but the turn ends
# promptly. Fast endpoints (deepseek closes in ~0ms) are unaffected.
USAGE_TAIL_TIMEOUT = 3.0

# HTTP-level timeouts for the AsyncOpenAI client. connect fails fast on dead
# endpoints. read must cover slow first-token endpoints — Qwen and reasoning
# models (and some OpenAI-compatible gateways) can take well over a minute
# before sending even the response headers, so the old flat 60s produced
# spurious APITimeoutErrors on perfectly healthy requests. Keep read above
# IDLE_TIMEOUT so a mid-stream stall is judged by the per-chunk idle guard,
# not by httpx.
CONNECT_TIMEOUT = 15.0
READ_TIMEOUT = 300.0


class OpenAIProvider:
    """LLMProvider backed by OpenAI Chat Completions API."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "",
        max_tokens: int = 32768,
        provider: str = "",
    ):
        self._key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._base_url = base_url
        self._model = model
        self._max_tokens = max_tokens
        self._provider = provider
        self._dialect = detect_dialect(model, base_url, provider)
        # Selected reasoning effort (a raw provider value such as "high"/"max"/
        # "none"). None means no reasoning control — no fields are added.
        self.reasoning_effort: str | None = None
        self._client = self._make_client()

    def set_reasoning_effort(self, effort: str | None) -> None:
        """Set the reasoning effort used by subsequent ``chat()`` calls.

        Accepts a raw provider value, ``None``, or the ``"default"`` sentinel —
        the latter two both mean "send no reasoning fields". The value lives on
        the provider (shared across turns/subagents), so the jsonrpc server
        mutates it in place when the user switches effort in the composer.
        """
        self.reasoning_effort = coerce_effort(effort)

    def _make_client(self) -> AsyncOpenAI:
        kwargs: dict[str, Any] = {
            # Positional arg is the default for read/write/pool; connect is
            # overridden to fail fast on unreachable endpoints.
            "timeout": httpx.Timeout(
                READ_TIMEOUT, connect=CONNECT_TIMEOUT, write=60.0, pool=30.0
            ),
            "max_retries": 0,
        }
        if self._key:
            kwargs["api_key"] = self._key
        if self._base_url:
            kwargs["base_url"] = self._base_url
        return AsyncOpenAI(**kwargs)

    def reset(self) -> AsyncOpenAI:
        """Rebuild the async client so it binds to the current event loop.

        The JSON-RPC server runs each turn in a fresh asyncio loop that it
        closes at turn end. httpx (under AsyncOpenAI) binds its connection pool
        to the loop of the FIRST request, so a client reused across turns would
        be tied to a closed loop and hang on the next turn. Rebuilding per turn
        keeps the client bound to the live loop.

        Returns the new client. The caller must pass it back to aclose() at turn
        end — see below.
        """
        self._client = self._make_client()
        return self._client

    async def aclose(self, client: AsyncOpenAI | None = None) -> None:
        """Close a client's connection pool on the current live loop.

        Closes ``client`` (default: the provider's current ``_client``). Turns
        can overlap — a Stop only cancels at the next approval boundary, so an
        in-flight stream keeps running while the next send starts a new turn on
        a fresh loop — and both turns share ONE provider instance. Closing the
        provider's *current* client in the old turn's teardown would instead
        close whichever client the new turn just reset to, killing the new
        turn's stream with an httpx.ReadError. So the server holds the client
        ``reset()`` returned and closes exactly that one here.

        The turn's loop closes right after this returns; letting the GC finalize
        the httpx pool later would run on that dead loop and raise 'Event loop is
        closed'. Errors here are non-fatal — the client may hold no open pool.
        """
        target = client if client is not None else self._client
        try:
            await target.close()
        except Exception:
            pass

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        on_thinking: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self._model or "gpt-5.1",
            "max_completion_tokens": self.max_tokens(),
            "messages": self._normalize_messages(messages),
        }
        if tools:
            kwargs["tools"] = self._convert_tools(tools)
        extra = self._reasoning_extra_body()
        if extra:
            kwargs["extra_body"] = extra

        if on_delta is not None:
            return await self._translate_errors(
                self._chat_streaming(kwargs, on_delta, on_thinking)
            )
        return await self._translate_errors(self._chat_blocking(kwargs))

    def _reasoning_extra_body(self) -> dict[str, Any]:
        """Build the provider-specific reasoning fields for the request body.

        The raw value is translated to the endpoint's wire shape per its dialect
        (see cluxmate/core/reasoning.py::wire_for): plain OpenAI-compatible
        endpoints take ``reasoning_effort`` directly, while DeepSeek/GLM/Qwen
        wrap the level in ``thinking:{type:enabled}`` and ``none``/``off``
        disable thinking. Sent through ``extra_body`` so every endpoint —
        including strict gateways — sees the provider-specific fields.
        """
        if self.reasoning_effort is None:
            return {}
        return wire_for(self._dialect, self.reasoning_effort)

    async def _translate_errors(self, coro):
        """Convert SDK failures into the two unified exception types.

        Only pure transport failures (TCP/DNS/SSL, request timeout) become
        ``LLMNetworkError`` — the call never reached the API so there is no
        provider message.  Every other SDK error — ``APIStatusError`` (HTTP
        error responses) and ``APIError`` (includes the DashScope / Qwen
        SSE-in-frame error path where the HTTP transport was 200) — becomes
        ``LLMProviderError`` carrying the cleaned provider message, which the
        agent loop shows to the user verbatim.
        """
        try:
            return await coro
        except LLMProviderError:
            raise  # already unified
        except (APITimeoutError, httpx.TimeoutException) as e:
            raise asyncio.TimeoutError("LLM API request timed out") from e
        except APIStatusError as e:
            raise LLMProviderError(
                _extract_provider_message(e.body) or e.message
            ) from e
        except (APIConnectionError, httpx.RequestError) as e:
            # TCP/DNS/SSL failure, or a stream dropped mid-body. The SDK wraps
            # the initial request as APIConnectionError, but errors raised while
            # iterating the SSE body (httpx.ReadError and the rest of the
            # TransportError family, e.g. anyio.ClosedResourceError after a
            # connection is torn down) surface bare and never become an SDK
            # exception — catch them here too. The call never completed, so it is
            # a pure transport failure with no provider message worth showing.
            raise LLMNetworkError(str(e)) from e
        except APIError as e:
            # Any other SDK-level error carrying a provider body (includes
            # the DashScope / Qwen SSE-in-frame error path).
            raise LLMProviderError(
                _extract_provider_message(e.body) or e.message
            ) from e

    async def _chat_blocking(self, kwargs: dict[str, Any]) -> LLMResponse:
        # One retry with backoff for transient connection errors (TCP reset,
        # DNS blip, SSL renegotiation). Timeouts are NOT retried — the read
        # window is already generous, so a silent server would just double
        # the wait. NOTE: the SDK's APITimeoutError SUBCLASSES
        # APIConnectionError, so the timeout clause must come FIRST or
        # timeouts take the retry path and surface as raw SDK errors.
        last_err: Exception | None = None
        for attempt in range(2):
            try:
                resp = await asyncio.wait_for(
                    self._client.chat.completions.create(**kwargs),
                    timeout=180.0,
                )
                break
            except (APITimeoutError, httpx.TimeoutException):
                raise asyncio.TimeoutError("LLM API request timed out")
            except asyncio.TimeoutError:
                raise asyncio.TimeoutError("LLM API request timed out (provider-level)")
            except APIConnectionError as e:
                last_err = e
                if attempt == 0:
                    await asyncio.sleep(2.0)
        else:
            raise last_err  # type: ignore[misc]  # always set when loop exits without break
        choice = resp.choices[0]
        msg = choice.message

        usage = getattr(resp, "usage", None)
        in_tok = getattr(usage, "prompt_tokens", None) if usage else None
        out_tok = getattr(usage, "completion_tokens", None) if usage else None
        # OpenAI prompt-cache tokens live under usage.prompt_tokens_details.
        ptd = getattr(usage, "prompt_tokens_details", None) if usage else None
        cache_read = getattr(ptd, "cached_tokens", None) if ptd else None
        cache_write = getattr(ptd, "cache_write_tokens", None) if ptd else None

        calls = None
        if msg.tool_calls:
            calls = []
            for tc in msg.tool_calls:
                parsed, err = parse_tool_arguments(tc.function.arguments)
                calls.append(ToolCall(
                    id=tc.id, name=tc.function.name, input=parsed, parse_error=err,
                ))

        stop = choice.finish_reason or "stop"
        return self._assemble(msg.content, calls, stop, in_tok, out_tok, cache_read, cache_write)

    async def _chat_streaming(
        self,
        kwargs: dict[str, Any],
        on_delta: Callable[[str], Awaitable[None]],
        on_thinking: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        # include_usage adds a final chunk carrying token counts (the per-delta
        # chunks have usage=None).
        kwargs = {**kwargs, "stream": True, "stream_options": {"include_usage": True}}

        # Retry a transient connection failure ONLY before we've forwarded any
        # text — once a delta has reached the UI we can't cleanly un-emit it, so
        # a mid-stream drop must surface rather than replay from the top.
        last_err: Exception | None = None
        for attempt in range(2):
            emitted = False
            text_parts: list[str] = []
            # index -> {id, name, args} accumulated across chunks
            tool_acc: dict[int, dict[str, str]] = {}
            finish: str | None = None
            in_tok: int | None = None
            out_tok: int | None = None
            cache_read: int | None = None
            cache_write: int | None = None
            try:
                # The wait for response headers (slow first-token models) is
                # bounded by the client's httpx read timeout (READ_TIMEOUT);
                # gaps between chunks once streaming starts are bounded by
                # IDLE_TIMEOUT below.
                stream = await self._client.chat.completions.create(**kwargs)
                agen = stream.__aiter__()
                while True:
                    # After finish_reason, only the usage tail remains — bound
                    # the wait tightly so a slow-to-close stream can't leave the
                    # turn hanging with the answer already rendered. Before that,
                    # the full idle window covers legitimate mid-stream gaps.
                    have_usage = in_tok is not None or cache_read is not None
                    if finish is not None:
                        if have_usage:
                            break  # answer + usage both in hand; don't wait for close
                        chunk_timeout = USAGE_TAIL_TIMEOUT
                    else:
                        chunk_timeout = IDLE_TIMEOUT
                    try:
                        chunk = await asyncio.wait_for(
                            agen.__anext__(), timeout=chunk_timeout
                        )
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        # A timeout AFTER finish_reason just means the trailing
                        # usage chunk never came — the answer is complete, so end
                        # the turn normally (without cache stats) instead of
                        # raising. Before finish_reason this is a real stall and
                        # must propagate to the idle-guard handler below.
                        if finish is not None:
                            break
                        raise
                    # A usage-only trailing chunk may carry no choices.
                    usage = getattr(chunk, "usage", None)
                    if usage is not None:
                        in_tok = getattr(usage, "prompt_tokens", in_tok)
                        out_tok = getattr(usage, "completion_tokens", out_tok)
                        ptd = getattr(usage, "prompt_tokens_details", None)
                        if ptd is not None:
                            cache_read = getattr(ptd, "cached_tokens", cache_read)
                            cache_write = getattr(ptd, "cache_write_tokens", cache_write)
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    if choice.finish_reason:
                        finish = choice.finish_reason
                    delta = choice.delta
                    if delta is None:
                        continue
                    # reasoning_content comes from DeepSeek R1 and similar
                    # models that emit thought tokens separately from content.
                    # These are forwarded through on_thinking and NOT appended
                    # to text_parts so they stay out of the final text.
                    reasoning = getattr(delta, 'reasoning_content', None)
                    if reasoning and on_thinking:
                        await on_thinking(reasoning)
                    if delta.content:
                        text_parts.append(delta.content)
                        emitted = True
                        await on_delta(delta.content)
                    for tc in (delta.tool_calls or []):
                        slot = tool_acc.setdefault(
                            tc.index, {"id": "", "name": "", "args": ""}
                        )
                        if tc.id:
                            slot["id"] = tc.id
                        fn = tc.function
                        if fn is not None:
                            if fn.name:
                                slot["name"] = fn.name
                            if fn.arguments:
                                slot["args"] += fn.arguments
                # Loop exited — either the stream closed on its own
                # (StopAsyncIteration) or we broke early with the answer (+ maybe
                # usage) already in hand. Close the response explicitly so an
                # early break releases the HTTP connection now instead of leaving
                # a half-read SSE body pinned until the turn-end aclose(). Safe if
                # already closed; best-effort.
                try:
                    await stream.close()
                except Exception:
                    pass
                break
            except (APITimeoutError, httpx.TimeoutException):
                raise asyncio.TimeoutError("LLM API request timed out")
            except asyncio.TimeoutError:
                raise asyncio.TimeoutError("LLM streaming stalled (idle timeout)")
            except (APIConnectionError, httpx.RequestError) as e:
                # A mid-body httpx.ReadError (connection dropped while streaming)
                # escapes the SDK wrapper, so it must be caught here directly to
                # get the same retry-before-emitted treatment as a connect error.
                last_err = e
                if attempt == 0 and not emitted:
                    await asyncio.sleep(2.0)
                    continue
                raise
        else:
            raise last_err  # type: ignore[misc]

        calls = None
        if tool_acc:
            calls = []
            for _, slot in sorted(tool_acc.items()):
                parsed, err = parse_tool_arguments(slot["args"])
                calls.append(ToolCall(
                    id=slot["id"], name=slot["name"], input=parsed, parse_error=err,
                ))

        text = "".join(text_parts)
        return self._assemble(text, calls, finish or "stop", in_tok, out_tok, cache_read, cache_write)

    def _assemble(
        self,
        content: str | None,
        calls: list[ToolCall] | None,
        finish: str,
        in_tok: int | None,
        out_tok: int | None,
        cache_read: int | None = None,
        cache_write: int | None = None,
    ) -> LLMResponse:
        if calls:
            return LLMResponse(
                text=content or None,
                tool_calls=calls,
                stop_reason="tool_use",
                input_tokens=in_tok,
                output_tokens=out_tok,
                cache_read_input_tokens=cache_read,
                cache_creation_input_tokens=cache_write,
            )
        if finish in TOOL_FINISH:
            stop_reason = "tool_use"
        elif finish == "length":
            # Output budget exhausted. Thinking models (Qwen3, R1, …) charge
            # reasoning tokens against the SAME budget, so a "length" finish
            # can carry no visible reply at all — the reasoning ate everything
            # and the completion was cut off mid-thought. Report it as
            # max_tokens so the agent loop surfaces it instead of rendering a
            # silent empty bubble.
            stop_reason = "max_tokens"
        else:
            stop_reason = "end_turn"
        return LLMResponse(
            text=content or "", stop_reason=stop_reason,
            input_tokens=in_tok, output_tokens=out_tok,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        )

    def _normalize_messages(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Repair message shapes strict OpenAI-compatible APIs reject.

        DeepSeek 400s on assistant messages whose content is null (OpenAI
        itself tolerates it); it requires a string, empty is fine. History
        persisted before assistant_message_to_api stopped emitting nulls may
        still carry them — the desktop replays saved history each turn — so
        fix at the request boundary, not only at construction. Copies dicts
        it repairs; the caller's list is left untouched.
        """
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.get("role") == "assistant" and m.get("content") is None:
                m = {**m, "content": ""}
            out.append(m)
        return out

    def assistant_message_to_api(
        self, msg: AssistantMessage
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"role": "assistant"}
        # DeepSeek (and other strict OpenAI-compatible endpoints) 400 with
        # "The content field is a required field." on content: null — they
        # require a string, empty is fine. OpenAI itself accepts both, so ""
        # is the portable choice for a text-less (tool-calls-only) turn.
        result["content"] = msg.text or ""
        if msg.tool_calls:
            result["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.input, ensure_ascii=False),
                    },
                }
                for tc in msg.tool_calls
            ]
        return result

    def tool_result_to_api(
        self, result: ToolResultMessage
    ) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": result.tool_call_id,
            # Same null-content rule as assistant messages: keep it a string.
            "content": result.content or "",
        }

    def max_tokens(self) -> int:
        return self._max_tokens

    def _convert_tools(
        self, tools: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        # BaseTool.definition() returns {name, description, input_schema}.
        # OpenAI's API expects "parameters", not "input_schema" — sending the
        # schema under the wrong field name would cause a 400 or silently strip
        # the parameter schema from the model's view.
        converted = []
        for t in tools:
            fn = {k: v for k, v in t.items() if k != "input_schema"}
            fn["parameters"] = t.get("input_schema", {"type": "object"})
            converted.append({"type": "function", "function": fn})
        return converted
