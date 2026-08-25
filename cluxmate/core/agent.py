"""AgentLoop — the core sampler loop."""

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from cluxmate.core.providers.base import (
    AssistantMessage,
    LLMProvider,
    LLMProviderError,
    LLMNetworkError,
    LLMResponse,
    ToolCall,
    ToolResultMessage,
)
from cluxmate.core.context import compact, estimate_tokens
from cluxmate.core.session_log import (
    APPEND,
    STAGE_APPROVAL,
    STAGE_STREAMING,
    STAGE_TOOL_EXECUTING,
    ReplaceOp,
    SessionLog,
    canonical_header,
    fold_request_header,
    header_equals,
)
from cluxmate.tools.base import ToolBridge, ToolResult
from cluxmate.tools._sandbox import validate_escalation_args
from cluxmate.core.hooks import HookManager

# Compact when the running context estimate exceeds this fraction of the window.
COMPACT_THRESHOLD = 0.8

# Friendly fallback shown when the provider call fails for anything other than
# quota exhaustion: network unreachable, model unavailable, request timeouts,
# auth/server errors. Quota errors (LLMQuotaError) surface the provider's own
# message instead.
NETWORK_FALLBACK_TEXT = "网络异常，请稍后重试"

# Synthetic tool-result content + error codes for tools whose outcome was never
# durably recorded because the turn stopped before they ran. Written by the
# writer-side self-closure so an aborted turn's surface stays a valid transcript
# and the audit trail answers "was this tool denied, cancelled, or executed?".
TOOL_DENIED_TEXT = "[Tool execution denied by user]"
TOOL_CANCELLED_TEXT = "[Tool cancelled: the turn was stopped before this tool finished]"
TOOL_DENIED_ERROR = {"name": "denied", "code": "TOOL_DENIED"}
TOOL_CANCELLED_ERROR = {"name": "cancelled", "code": "TOOL_CANCELLED"}
TOOL_MALFORMED_ERROR = {"name": "malformed", "code": "TOOL_MALFORMED_ARGS"}
TOOL_EXEC_ERROR = {"name": "error", "code": "TOOL_ERROR"}
TOOL_HOOK_BLOCKED_ERROR = {"name": "hook_blocked", "code": "TOOL_HOOK_BLOCKED"}

def _interruption_marker(turn: int, reason: dict[str, Any]) -> str | None:
    """Neutral English marker for the turn AFTER an aborted/interrupted turn.

    Returns None for completed/max-tokens/max-turns/error turns — those already
    have a real assistant reply in the log, so no extra signal is needed.
    """
    kind = reason.get("kind")
    if kind not in ("aborted", "interrupted"):
        return None
    stage = reason.get("stage") or "unknown"
    step = reason.get("step")
    loc = f"turn {turn}"
    if step is not None:
        loc += f", step {step}"
    if kind == "aborted":
        return (
            f"[The previous turn was stopped by the user and did not complete"
            f" (interrupted during {stage}, {loc})."
            f" Do not treat its partial work as finished.]"
        )
    return (
        f"[The previous turn was interrupted by a process termination and did not"
        f" complete (interrupted during {stage}, {loc})."
        f" Some tool outcomes may be unknown — verify state before retrying"
        f" anything with side effects.]"
    )


def _tool_result_error(status: str, is_error: bool) -> dict[str, str] | None:
    """The ``error`` field for a tool result, from its terminal disposition."""
    if status == "denied":
        return TOOL_DENIED_ERROR
    if status == "malformed":
        return TOOL_MALFORMED_ERROR
    if status == "cancelled":
        return TOOL_CANCELLED_ERROR
    if status == "hook_blocked":
        return TOOL_HOOK_BLOCKED_ERROR
    if status == "executed" and is_error:
        return TOOL_EXEC_ERROR
    return None


def _canonicalize(value: Any) -> Any:
    """Deep key-sort a JSON value so two argument objects that differ only in
    property order canonicalize identically. Arguments reach the guard as
    parsed-JSON dicts (or their raw-string fallback for malformed JSON), so
    JSON's value domain is the whole input domain."""
    if isinstance(value, dict):
        return {k: _canonicalize(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_canonicalize(v) for v in value]
    return value


def _canonical_args(args: dict[str, Any]) -> str:
    """Canonical string form of a tool call's arguments (deep key-sort + JSON)."""
    return json.dumps(_canonicalize(args), ensure_ascii=False, default=str)


# Debug aid: when CLUXMATE_DEBUG_REQUESTS is truthy, dump each outgoing LLM
# request (messages + tools) to stderr. stderr is used so the dump never corrupts
# the TUI screen or the JSON-RPC stdout channel. Each field is clipped to
# CLUXMATE_DEBUG_MAX_CHARS characters (default 4000).
_DEBUG_REQUESTS = os.environ.get("CLUXMATE_DEBUG_REQUESTS", "").lower() not in (
    "", "0", "false", "no", "off",
)
_MAX_DUMP_CHARS = int(os.environ.get("CLUXMATE_DEBUG_MAX_CHARS", "4000"))


def _clip_dump(text: Any) -> str:
    text = str(text)
    if len(text) > _MAX_DUMP_CHARS:
        return text[: _MAX_DUMP_CHARS] + f"\n…[+{len(text) - _MAX_DUMP_CHARS} chars]"
    return text


def _dump_content(content: Any) -> str:
    if isinstance(content, str):
        return _clip_dump(content)
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif isinstance(block.get("content"), str):
                    parts.append(block["content"])
                elif block.get("type"):
                    parts.append(f"<{block['type']} block>")
        return _clip_dump("\n".join(parts))
    return _clip_dump(content)


def _dump_request(
    turn: int | None, step: int | None,
    messages: list[dict[str, Any]], tools: list[dict[str, Any]],
) -> None:
    sep = "=" * 78
    sys.stderr.write(f"\n{sep}\n")
    sys.stderr.write(
        f"OUTGOING LLM REQUEST  turn={turn} step={step}  "
        f"{len(messages)} messages, {len(tools)} tools\n"
    )
    sys.stderr.write(f"{sep}\n")
    for i, m in enumerate(messages):
        sys.stderr.write(f"[{i}] {m.get('role', '?')}\n{_dump_content(m.get('content'))}\n")
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            sys.stderr.write(
                f"    -> tool_call {fn.get('name', '?')}({_clip_dump(fn.get('arguments', ''))})\n"
            )
    sys.stderr.write(f"{sep}\nTOOLS ({len(tools)})\n")
    for t in tools:
        sys.stderr.write(f"  * {t.get('name', '?')}: {_clip_dump(t.get('description', ''))}\n")
    sys.stderr.write(f"{sep}\n")
    sys.stderr.flush()


class AgentCallbacks:
    """Callback hooks for the agent loop. All methods are optional no-ops by default."""

    async def on_tool_start(
        self, name: str, params: dict[str, Any], call_id: str, risk_level: str
    ) -> bool:
        """Called before tool execution. Return True to proceed, False to skip."""
        return True

    async def on_tool_end(
        self, call_id: str, result: "ToolResult"
    ) -> None:
        """Called after tool execution with the result."""
        pass

    async def on_text_delta(self, chunk: str) -> None:
        """Called with each incremental text chunk as the model streams output.

        Providers that support streaming forward every token/segment here so the
        UI can render text as it arrives. Non-streaming providers emit the whole
        reply as a single chunk. The concatenation of all chunks equals the
        turn's final text, so consumers must not ALSO append on_text output.
        """
        pass

    async def on_thinking_delta(self, chunk: str) -> None:
        """Called with each incremental reasoning/thinking token.

        Distinct from on_text_delta — thinking content is rendered in a separate
        collapsible panel in the UI and must NOT be mixed into the final text.
        """
        pass

    async def on_text_restart(self) -> None:
        """Called when a generation attempt is discarded and restarted (e.g. a
        Stop hook blocked the reply). The UI should clear the previously-streamed
        text/thinking so the new stream starts fresh instead of appending to the
        rejected attempt.
        """
        pass

    async def get_tool_selection(self, call_id: str) -> list[int] | None:
        """Return the user's selected edit indices, or None if not applicable."""
        return None

    async def ask_question(
        self, questions: list[dict[str, Any]], call_id: str
    ) -> dict[str, Any] | None:
        """Ask the user one batch of questions and block until they answer.

        Returns ``{"answers": [{"id", "selected", "custom?"}]}`` on success, or
        None when this frontend cannot ask (the tool then returns an unsupported
        error). Raises the frontend's cancellation exception on cancel, mirroring
        ``on_tool_start``.
        """
        return None


@dataclass
class AgentResult:
    """Result from running an agent turn."""

    text: str | None = None
    history: list[dict[str, Any]] | None = field(default_factory=list)
    tool_calls_made: int = 0
    # Accumulated prompt-cache token counts across all LLM calls in this turn.
    # None when neither provider returned cache details.
    cache_usage: dict[str, int] | None = None
    # Timing for the "first token latency" / "token rate" UI. Both cover ONLY
    # model inference time: waiting for tool approval and tool execution is
    # excluded so the rate reflects generation speed, not how long the user took
    # to click Approve or how long a bash command ran.
    #   ttft_ms:   wall-clock from the first LLM request until its first output
    #              token (text, thinking, or a tool_use) arrives. None if the
    #              provider is non-streaming (no per-token signal).
    #   gen_ms:    total model-generation time across all LLM calls this turn
    #              (each call's first-token → last-token).
    #   out_tokens: total output tokens across all LLM calls this turn.
    ttft_ms: int | None = None
    gen_ms: int = 0
    out_tokens: int = 0


class AgentLoop:
    """Core agent loop — call LLM, execute tools, repeat.

    Both parent and child agents use the same implementation.
    Set `parent` to make this a child agent.
    """

    MAX_TURNS = 150  # Safety limit to prevent infinite loops
    # How many times to silently re-issue a request that came back as an empty
    # end_turn (reasoning-model quirk) before giving up with the marker text.
    # One retry catches the common transient miss (an identical re-issue almost
    # always yields a real answer) while bounding the extra latency the user
    # waits through — each retry is a full LLM call — to a single round.
    MAX_EMPTY_END_TURN_RETRIES = 1
    # How many times a Stop hook may reject (block) a reply before the loop
    # gives up and returns the last attempt as-is. Each rejection re-runs the
    # model with the hook's reason, so this bounds the extra latency a
    # pathological always-blocking hook can impose.
    MAX_STOP_BLOCK_RETRIES = 3

    # Doom-loop guard — modeled on DeepSeek Harness's repeat-tool-reminder.
    # Consecutive identical tool calls (same name + canonical arguments) trigger
    # escalating advisory reminders at these run lengths. The FIRST threshold
    # gets a gentle nudge; later thresholds get the detailed form. Advisory
    # only: the decision to change approach or finish stays with the model,
    # with MAX_TURNS as the hard backstop.
    REPEAT_THRESHOLDS = (3, 6, 9)
    # Bookkeeping tools transparent to the chain: they neither count nor reset
    # the consecutive-run counter, so an interleaved use_skill/update_memory
    # cannot launder a loop.
    REPEAT_EXCLUDE = frozenset({"use_skill", "update_memory"})
    # Cap on the canonical-arguments preview in the DETAILED reminder; the chain
    # key always compares the full canonical string, this only bounds the
    # model-visible text so a looping write/edit payload can't ride unbounded.
    REPEAT_ARGS_PREVIEW_CHARS = 500

    def __init__(
        self,
        model: str,
        provider: LLMProvider,
        tools: ToolBridge,
        system_prompt: str,
        parent: "AgentLoop | None" = None,
        context_window: int = 128_000,
        session_log: SessionLog | None = None,
        mode: str = "default",
        hooks: HookManager | None = None,
    ):
        self.model = model
        self.provider = provider
        self.tools = tools
        self.system_prompt = system_prompt
        self.parent = parent
        self.context_window = context_window
        # Lifecycle hooks (user-configured commands). None disables the feature.
        # Inherited by child agents (see build_child) so subagent tool calls are
        # hooked too.
        self.hooks = hooks
        # Optional event-sourced log. When set, each run() emits turn/step/user/
        # assistant/tool/request-header events. The working `messages` list stays
        # dual-written in parallel; the log is the append-only historical record.
        self.session_log = session_log
        # Development mode ("default" | "plan" | "acceptEdits" | "yolo") — logged
        # in request/header.config so a mid-session mode switch is an explicit,
        # diffable change rather than only an opaque system-prompt text change.
        self.mode = mode
        # Current turn/step, set only while a logged run() is in flight so the
        # per-step helpers know where to place assistant/tool events.
        self._log_turn: int | None = None
        self._log_step: int | None = None
        # Set True when compaction rewrote the message surface this turn; the
        # caller uses it to re-inject environment state (memory/skill/mode) on
        # the next turn (see AgentBuilder.invalidate_injections).
        self.compacted_this_turn = False
        # Per-turn audit state (reset at each run()): the current interruption
        # stage and the disposition of every tool call this turn, so a turn that
        # aborts can self-close its orphaned tool calls with an honest result.
        self._log_stage: str | None = None
        self._log_tool_meta: dict[str, dict[str, Any]] = {}
        # Doom-loop guard chain (per AgentLoop instance, so parent/child chains
        # stay isolated): the canonical identity key of the last tracked call
        # and its consecutive run length.
        self._repeat_key: str | None = None
        self._repeat_count = 0

    # ── session-log helpers ────────────────────────────────────────────────

    def _log_append(
        self, type: str, data: dict[str, Any], *, surface_op: Any = None
    ) -> None:
        if self.session_log is not None:
            self.session_log.append(type, data, surface_op=surface_op)

    def _log_assistant(
        self,
        api_msg: dict[str, Any],
        *,
        usage: dict[str, Any] | None = None,
        reasoning: str | None = None,
        timing: dict[str, Any] | None = None,
    ) -> None:
        if self.session_log is None:
            return
        data: dict[str, Any] = {
            "turn": self._log_turn,
            "step": self._log_step,
            "message": api_msg,
        }
        if usage is not None:
            data["usage"] = usage
        if reasoning:
            data["reasoning"] = reasoning
        if timing is not None:
            data["timing"] = timing
        self.session_log.append("assistant/message", data, surface_op=APPEND)

    def _log_tool_result(
        self,
        call_id: str,
        api_msg: dict[str, Any],
        *,
        error: dict[str, str] | None = None,
        turn: int | None = None,
        step: int | None = None,
    ) -> None:
        if self.session_log is None:
            return
        data: dict[str, Any] = {
            "turn": self._log_turn if turn is None else turn,
            "step": self._log_step if step is None else step,
            "callId": call_id,
            "message": api_msg,
        }
        if error is not None:
            data["error"] = error
        self.session_log.append("tool/result", data, surface_op=APPEND)
        meta = self._log_tool_meta.get(call_id)
        if meta is not None:
            meta["logged"] = True

    def _log_compaction(self, edit: tuple[int, int, list[dict[str, Any]]]) -> None:
        """Record a compaction as a single surface-replace event.

        ``edit`` is ``(msg_start, msg_end, replacement)`` from ``context.compact``:
        messages ``[msg_start, msg_end)`` were replaced by ``replacement`` (one
        summary/truncation message). ``messages[0]`` is the system message
        (excluded from the surface), so a message index ``i`` maps to surface
        index ``i - 1``. ``source_event_seqs`` locks down which prior surface
        events this op shadowed, keeping the log replayable.
        """
        if self.session_log is None:
            return
        msg_start, msg_end, replacement = edit
        start = msg_start - 1
        end = msg_end - 2  # inclusive: ReplaceOp swaps surface[start..end] for one node
        surface = self.session_log.surface
        if start < 0 or end < start or end >= len(surface) or len(replacement) != 1:
            raise ValueError(
                f"invalid compaction edit {edit!r} against a surface of length {len(surface)}"
            )
        shadowed = [surface[i].seq for i in range(start, end + 1)]
        self.session_log.append(
            "user/message",
            {"message": replacement[0], "source": "compaction"},
            surface_op=ReplaceOp(start=start, end=end),
            source_event_seqs=shadowed,
        )

    def _last_turn_end(self) -> tuple[int, dict[str, Any]] | None:
        """``(turn, reason)`` of the most recent ``turn/end`` event, if any."""
        if self.session_log is None:
            return None
        for event in reversed(self.session_log.events):
            if event.type == "turn/end":
                return event.data.get("turn", 0), event.data.get("reason") or {}
        return None

    def _result_to_api(
        self, call_id: str, name: str, result: "ToolResult"
    ) -> dict[str, Any]:
        msg = ToolResultMessage(
            tool_call_id=call_id, content=result.content, name=name
        )
        return self.provider.tool_result_to_api(msg)

    def _close_orphaned_tools(self) -> None:
        """Writer-side self-closure for an aborted turn.

        Every ``tool/call`` with no logged ``tool/result`` gets a terminal result
        (in tool_calls order) so the surface stays a valid transcript and the
        audit trail answers "was this tool denied, cancelled, or executed?".
        Tools whose result was captured before the stop keep that real result;
        the rest get an honest "cancelled before it finished" marker.
        """
        if self.session_log is None:
            return
        for call_id, meta in self._log_tool_meta.items():
            if meta.get("logged"):
                continue
            status = meta.get("status", "pending")
            result = meta.get("result")
            if result is not None:
                api_msg = self._result_to_api(call_id, meta["name"], result)
                error = _tool_result_error(status, bool(result.is_error))
            else:
                api_msg = self._result_to_api(
                    call_id,
                    meta["name"],
                    ToolResult(
                        tool_call_id=call_id,
                        name=meta["name"],
                        content=TOOL_CANCELLED_TEXT,
                        is_error=True,
                    ),
                )
                error = TOOL_CANCELLED_ERROR
            self._log_tool_result(
                call_id,
                api_msg,
                error=error,
                turn=meta.get("turn"),
                step=meta.get("step"),
            )

    def _repeat_reminder(self, name: str, args: dict[str, Any]) -> str | None:
        """Advance the doom-loop chain for one tracked tool call.

        Returns the advisory reminder text when the call's consecutive-run
        length hits a configured threshold, else None. ``REPEAT_EXCLUDE`` tools
        are transparent (neither counted nor resetting); any other call that
        differs from the previous tracked call resets the chain to 1.
        """
        if name in self.REPEAT_EXCLUDE:
            return None
        key = json.dumps(
            [name, _canonical_args(args)], ensure_ascii=False, default=str
        )
        if key == self._repeat_key:
            self._repeat_count += 1
        else:
            self._repeat_key = key
            self._repeat_count = 1
        if self._repeat_count not in self.REPEAT_THRESHOLDS:
            return None
        if self._repeat_count == self.REPEAT_THRESHOLDS[0]:
            return (
                "You are repeating the exact same tool call with identical "
                "arguments. Carefully analyze the previous result before calling "
                "again: if the task is not complete, try a different approach or "
                "different arguments instead of repeating the call."
            )
        canon = _canonical_args(args)
        if len(canon) > self.REPEAT_ARGS_PREVIEW_CHARS:
            preview = (
                canon[: self.REPEAT_ARGS_PREVIEW_CHARS]
                + f"… (+{len(canon) - self.REPEAT_ARGS_PREVIEW_CHARS} more chars)"
            )
        else:
            preview = canon
        return (
            "Repeated tool call detected:\n"
            f"- tool: {name}\n"
            f"- consecutive_calls: {self._repeat_count}\n"
            f"- arguments: {preview}\n"
            "The repeated calls are not making progress. Do not call this tool "
            "with these exact arguments again. Inspect the latest result and "
            "choose a different action, different arguments, or finish the task "
            "if enough evidence has been gathered."
        )

    def _fail_turn(
        self,
        messages: list[dict[str, Any]],
        text: str,
        tool_calls_made: int,
        end_reason: dict[str, Any] | None = None,
    ) -> AgentResult:
        """End the turn with an error marker as the assistant reply.

        The marker is ALSO appended to history: it keeps user/assistant roles
        alternating (a failed turn otherwise leaves history ending on the user
        message, and two consecutive user messages 400 on Anthropic), and lets
        the model see what happened if the conversation continues.
        """
        if end_reason is not None:
            end_reason["kind"] = "error"
        assistant = AssistantMessage(text=text)
        api_msg = self.provider.assistant_message_to_api(assistant)
        messages.append(api_msg)
        self._log_assistant(api_msg)
        self._log_append(
            "step/end", {"turn": self._log_turn, "step": self._log_step}
        )
        return AgentResult(
            text=text,
            history=messages[1:],  # exclude system prompt
            tool_calls_made=tool_calls_made,
        )

    async def run(
        self,
        user_message: str,
        history: list[dict[str, Any]] | None = None,
        *,
        callbacks: AgentCallbacks | None = None,
        injections: list[tuple[str, str]] | None = None,
    ) -> AgentResult:
        """Run the agent loop for a single user message.

        Args:
            user_message: The user's input text.
            history: Previous conversation messages (API format).
            callbacks: Optional hooks for tool approval and streaming output.
            injections: Optional ``(source, content)`` synthetic user messages
                (e.g. project memory, skills) appended before the human message.

        Returns:
            AgentResult with final text and updated history.
        """
        self.compacted_this_turn = False
        self._log_stage = None
        self._log_tool_meta = {}
        # A new user message is a fresh context: repetition across turns is not
        # a loop, so reset the doom-loop chain at the turn boundary.
        self._repeat_key = None
        self._repeat_count = 0
        log = self.session_log

        # UserPromptSubmit hook: fires before the prompt reaches the model. A
        # block rejects the prompt BEFORE turn/start is recorded — the prompt is
        # not added to history and no turn is logged. Feedback is prepended as a
        # source:"hook" environment injection (before the human message).
        block_reason: str | None = None
        if self.hooks is not None and self.hooks.has_event("UserPromptSubmit"):
            hr = await self.hooks.run_event("UserPromptSubmit", prompt=user_message)
            if hr.blocked:
                block_reason = hr.reason or "[Prompt blocked by hook]"
            elif hr.feedback:
                injections = list(injections or []) + [
                    ("hook", fb) for fb in hr.feedback
                ]
        if block_reason is not None:
            return AgentResult(
                text=block_reason,
                history=log.derive_messages() if log is not None else None,
            )

        if log is None:
            return await self._run_loop(user_message, history, callbacks, injections)

        # The session log is the single source of truth: any caller-provided
        # history must match the log surface, or replay and the live request
        # would diverge.
        if history is not None and history != log.derive_messages():
            raise ValueError(
                "history does not match the session log surface; "
                "pass SessionLog.derive_messages()"
            )

        session_turn = log.turn_count + 1
        self._log_turn = session_turn
        log.append("turn/start", {"turn": session_turn})

        # If the immediately-preceding turn did not complete (aborted/interrupted),
        # inject a neutral English marker so the model knows its partial work was
        # cut short and can decide whether to resume or follow the new request.
        interruption: list[tuple[str, str]] = []
        prev = self._last_turn_end()
        if prev is not None:
            marker = _interruption_marker(prev[0], prev[1])
            if marker:
                interruption = [("interruption", marker)]

        # Environment injections precede the human message (H ordering) so the
        # model sees its environment state before the request.
        for source, content in interruption + list(injections or []):
            log.append(
                "user/message",
                {"message": {"role": "user", "content": content}, "source": source},
                surface_op=APPEND,
            )
        log.append(
            "user/message",
            {"message": {"role": "user", "content": user_message}, "source": "human"},
            surface_op=APPEND,
        )
        end_reason: dict[str, Any] = {"kind": "completed"}
        try:
            return await self._run_loop(
                user_message, history, callbacks, injections, end_reason
            )
        except asyncio.CancelledError:
            end_reason["kind"] = "aborted"
            end_reason["reason"] = {"kind": "user"}
            end_reason["stage"] = self._log_stage or STAGE_STREAMING
            end_reason["step"] = self._log_step
            raise
        except Exception:
            # _run_loop returns (never raises) for every provider/tool failure, so
            # an exception escaping here is a cancellation: the approval-cancel
            # _CancelledError (a plain Exception in the JSON-RPC layer) or an
            # unexpected bug. Label it aborted so a cancelled turn never reads as
            # "completed" on replay.
            end_reason["kind"] = "aborted"
            end_reason["reason"] = {"kind": "user"}
            end_reason["stage"] = self._log_stage or STAGE_STREAMING
            end_reason["step"] = self._log_step
            raise
        finally:
            # Self-close any tool calls left without a result before the turn
            # closes, so an aborted turn's surface is still a valid transcript.
            self._close_orphaned_tools()
            log.append("turn/end", {"turn": session_turn, "reason": end_reason})
            self._log_turn = None
            self._log_step = None
            self._log_stage = None

    async def _run_loop(
        self,
        user_message: str,
        history: list[dict[str, Any]] | None,
        callbacks: AgentCallbacks | None,
        injections: list[tuple[str, str]] | None,
        end_reason: dict[str, Any] | None = None,
    ) -> AgentResult:
        messages = [{"role": "system", "content": self.system_prompt}]
        if history:
            messages.extend(history)
        for _source, content in (injections or []):
            messages.append({"role": "user", "content": content})
        messages.append({"role": "user", "content": user_message})

        tool_calls_made = 0
        # Accumulate per-LLM-call cache token counts across all turns so the UI
        # can show a single per-turn hit rate. Both may stay 0 when the provider
        # omits usage details. input_tokens_total tracks the total prompt tokens
        # across all LLM calls this turn so the hit rate denominator is correct
        # even when tool_use loops cause multiple calls.
        cache_read_total = 0
        cache_write_total = 0
        input_tokens_total = 0
        # Timing accumulators across all LLM calls this turn. first-token latency
        # is measured once (first call only); generation time and output tokens
        # accumulate so the rate covers the whole turn's actual generation.
        ttft_ms: int | None = None
        gen_ms_total = 0
        out_tokens_total = 0
        # Reasoning models (deepseek-v4-pro, R1, Qwen3, …) occasionally end a
        # turn having streamed only reasoning tokens and an EMPTY completion
        # (finish_reason=stop, no content, no tool calls). It's non-deterministic
        # — re-issuing the identical request usually yields a real answer. Retry
        # a bounded number of times before falling back to the "empty response"
        # marker, so the user isn't told to manually resend for a transient miss.
        empty_end_turn_retries = 0
        # Number of times a Stop hook has rejected (blocked) this turn's reply.
        # Capped by MAX_STOP_BLOCK_RETRIES; each block re-runs the model.
        stop_block_retries = 0
        # Running context-size estimate. Bootstrapped from chars; re-anchored to
        # the provider's exact usage after each response (see below).
        ctx_tokens = estimate_tokens(messages)
        limit = int(COMPACT_THRESHOLD * self.context_window)
        # Last request/header snapshot already in the log — the change-detection
        # baseline for the first request/header emitted this run.
        last_header = (
            fold_request_header(iter(self.session_log.events))
            if self.session_log is not None
            else None
        )

        for turn in range(self.MAX_TURNS):
            step = turn + 1
            # Compact before the call if we're over budget — inside the loop so a
            # single multi-tool turn can't overflow the window mid-turn.
            if ctx_tokens > limit:
                sources = None
                if self.session_log is not None:
                    sources = [None] + self.session_log.message_sources()
                messages, did, edit = await compact(
                    messages, self.context_window, self.provider,
                    threshold=COMPACT_THRESHOLD, sources=sources,
                )
                if did:
                    self.compacted_this_turn = True
                    self._log_compaction(edit)
                    ctx_tokens = estimate_tokens(messages)

            # Emit step/start, then a request/header snapshot only when the
            # non-history envelope (config/system/tools) changed since the last
            # logged header. The header is constant within a run(), so this fires
            # at most once per turn (reason "initial" or "change").
            if self.session_log is not None:
                self._log_step = step
                self.session_log.append(
                    "step/start", {"turn": self._log_turn, "step": step}
                )
                header = {
                    "config": {
                        "model": self.model,
                        "max_tokens": self.provider.max_tokens(),
                        "context_window": self.context_window,
                        "mode": self.mode,
                        # Reasoning effort is model-visible (it changes the request
                        # the provider sends), so it belongs in the logged envelope
                        # — a switch mid-session then surfaces as a `change` header.
                        "reasoning_effort": getattr(
                            self.provider, "reasoning_effort", None
                        ),
                    },
                    "system": self.system_prompt,
                    "tools": self.tools.definitions(),
                }
                if last_header is None:
                    reason = "initial"
                elif not header_equals(header, last_header):
                    reason = "change"
                else:
                    reason = None
                if reason:
                    self.session_log.append(
                        "request/header", {"header": header, "reason": reason}
                    )
                    last_header = canonical_header(header)

            # Stream text deltas to the UI when callbacks are attached. Passing
            # on_delta switches the provider to its streaming path; leaving it
            # None (no callbacks, e.g. the compaction summarizer) keeps the
            # non-streaming path. The provider owns the idle timeout — there is
            # deliberately no outer wall-clock cap here, since a long reply can
            # legitimately stream for longer than any fixed bound.
            on_delta = None
            on_thinking = None
            # Per-LLM-call generation timing. first_ts/last_ts are the wall-clock
            # instants of this call's first/last output token (text, thinking, or
            # a tool_use). Combined with call_start they yield first-token latency
            # and pure generation time — excluding tool approval/execution that
            # ran earlier in the turn (or between calls).
            first_ts: float | None = None
            last_ts: float | None = None
            # Accumulated thinking/reasoning deltas for this call — the provider
            # streams them via on_thinking but does not assemble LLMResponse.reasoning.
            reasoning_parts: list[str] = []
            if callbacks is not None:
                async def on_delta(chunk: str) -> None:
                    nonlocal first_ts, last_ts
                    now = time.monotonic()
                    if first_ts is None:
                        first_ts = now
                    last_ts = now
                    await callbacks.on_text_delta(chunk)

                async def on_thinking(chunk: str) -> None:
                    nonlocal first_ts, last_ts
                    now = time.monotonic()
                    if first_ts is None:
                        first_ts = now
                    last_ts = now
                    reasoning_parts.append(chunk)
                    await callbacks.on_thinking_delta(chunk)

            if _DEBUG_REQUESTS:
                _dump_request(
                    self._log_turn, self._log_step, messages, self.tools.definitions()
                )
            call_start = time.monotonic()
            self._log_stage = STAGE_STREAMING
            try:
                if on_delta is not None:
                    response = await self.provider.chat(
                        messages, self.tools.definitions(),
                        on_delta=on_delta, on_thinking=on_thinking,
                    )
                else:
                    response = await self.provider.chat(
                        messages, self.tools.definitions()
                    )
            except asyncio.TimeoutError:
                # Provider-level timeout or streaming idle guard — the call
                # never completed; no provider message to show.
                return self._fail_turn(
                    messages, NETWORK_FALLBACK_TEXT, tool_calls_made, end_reason
                )
            except LLMNetworkError:
                # TCP/DNS/SSL failure — same outcome as timeout.
                return self._fail_turn(
                    messages, NETWORK_FALLBACK_TEXT, tool_calls_made, end_reason
                )
            except LLMProviderError as e:
                # The API responded with an error body: quota, billing,
                # rate-limit, auth, model-not-found, server error, or a
                # DashScope SSE-in-frame error.  Show the cleaned provider
                # message to the user verbatim.
                return self._fail_turn(
                    messages,
                    e.provider_message or NETWORK_FALLBACK_TEXT,
                    tool_calls_made,
                    end_reason,
                )

            # Re-anchor the context estimate to the provider's exact usage when
            # available (input_tokens covers everything we just sent). Tool
            # results appended below add to this via char estimate.
            if response.input_tokens is not None:
                ctx_tokens = response.input_tokens + (response.output_tokens or 0)
                input_tokens_total += response.input_tokens
            # Accumulate cache token counts for the per-turn hit rate.
            if response.cache_read_input_tokens is not None:
                cache_read_total += response.cache_read_input_tokens
            if response.cache_creation_input_tokens is not None:
                cache_write_total += response.cache_creation_input_tokens

            # Fold this call's generation timing into the turn totals. First-token
            # latency is captured once (first call). For tool_use calls that emit
            # no text/thinking delta, no on_delta fires — the tool_use itself is
            # the first/last token; the whole call duration IS generation time
            # (no tool execution happens inside an LLM call), so fall back to
            # call_start → now for those calls.
            if ttft_ms is None and first_ts is not None:
                ttft_ms = int((first_ts - call_start) * 1000)
            if first_ts is not None and last_ts is not None:
                gen_ms_total += int((last_ts - first_ts) * 1000)
            elif first_ts is None and (response.output_tokens or 0) > 0:
                gen_ms_total += int((time.monotonic() - call_start) * 1000)
            if response.output_tokens is not None:
                out_tokens_total += response.output_tokens

            # Assemble per-step log metadata: reasoning, token usage, generation
            # timing for THIS LLM call.
            reasoning = "".join(reasoning_parts) if reasoning_parts else None
            usage = None
            if response.input_tokens is not None or response.output_tokens is not None:
                usage = {
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                    "cache_read": response.cache_read_input_tokens,
                    "cache_write": response.cache_creation_input_tokens,
                }
            step_ttft_ms = int((first_ts - call_start) * 1000) if first_ts is not None else None
            if first_ts is not None and last_ts is not None:
                step_gen_ms = int((last_ts - first_ts) * 1000)
            elif first_ts is None and (response.output_tokens or 0) > 0:
                step_gen_ms = int((time.monotonic() - call_start) * 1000)
            else:
                step_gen_ms = 0
            step_timing = {
                "ttft_ms": step_ttft_ms,
                "gen_ms": step_gen_ms,
                "out_tokens": response.output_tokens,
            }

            if response.stop_reason == "end_turn":
                text = response.text or ""
                if not text:
                    # An end_turn with nothing to show — a thinking model that
                    # streamed only reasoning and emitted an empty completion
                    # (finish_reason=stop, no content, no tool calls). This is
                    # transient: re-issue the SAME request (messages unchanged —
                    # we never appended the empty assistant turn) a bounded number
                    # of times before surfacing the marker. `continue` re-enters
                    # the loop without consuming a tool round or mutating history.
                    if empty_end_turn_retries < self.MAX_EMPTY_END_TURN_RETRIES:
                        empty_end_turn_retries += 1
                        self._log_append(
                            "step/end", {"turn": self._log_turn, "step": step}
                        )
                        continue
                    # Exhausted retries — give the UI (and the next turn's
                    # history) something honest instead of a silent empty bubble.
                    text = (
                        "[The model ended the turn with an empty response "
                        "(no text, no tool calls). Resend the message to retry.]"
                    )
                # Stop hook fires BEFORE the assistant message is committed, so a
                # block can reject this reply and re-run without polluting history
                # with the rejected text. It receives the full reply as `response`.
                if self.hooks is not None and self.hooks.has_event("Stop"):
                    hr = await self.hooks.run_event("Stop", response=text)
                    if hr.blocked and stop_block_retries < self.MAX_STOP_BLOCK_RETRIES:
                        stop_block_retries += 1
                        block_msg = {
                            "role": "user",
                            "content": hr.reason or "[Stop hook rejected this reply]",
                        }
                        messages.append(block_msg)
                        self._log_append(
                            "user/message",
                            {"message": block_msg, "source": "hook"},
                            surface_op=APPEND,
                        )
                        self._log_append(
                            "step/end", {"turn": self._log_turn, "step": step}
                        )
                        # Tell the UI to clear the rejected text before the new
                        # generation streams (else it would append/duplicate).
                        if callbacks is not None:
                            await callbacks.on_text_restart()
                        continue
                    for fb in hr.feedback:
                        fb_msg = {"role": "user", "content": fb}
                        messages.append(fb_msg)
                        self._log_append(
                            "user/message",
                            {"message": fb_msg, "source": "hook"},
                            surface_op=APPEND,
                        )
                assistant = AssistantMessage(text=text)
                api_msg = self.provider.assistant_message_to_api(assistant)
                messages.append(api_msg)
                self._log_assistant(
                    api_msg, usage=usage, reasoning=reasoning, timing=step_timing
                )
                self._log_append(
                    "step/end", {"turn": self._log_turn, "step": step}
                )
                # No on_text here: the streaming path already forwarded the full
                # text via on_text_delta chunks. Re-emitting would duplicate it.
                cache_usage = None
                if input_tokens_total:
                    cache_usage = {
                        "input_tokens": input_tokens_total,
                        "cache_read": cache_read_total,
                        "cache_write": cache_write_total,
                    }
                return AgentResult(
                    text=text,
                    history=messages[1:],  # exclude system prompt
                    tool_calls_made=tool_calls_made,
                    cache_usage=cache_usage,
                    ttft_ms=ttft_ms,
                    gen_ms=gen_ms_total,
                    out_tokens=out_tokens_total,
                )

            if response.stop_reason == "tool_use":
                tool_calls = response.tool_calls or []

                assistant = AssistantMessage(tool_calls=tool_calls)
                api_msg = self.provider.assistant_message_to_api(assistant)
                messages.append(api_msg)
                self._log_assistant(
                    api_msg, usage=usage, reasoning=reasoning, timing=step_timing
                )
                for tc in tool_calls:
                    tool_call_data: dict[str, Any] = {
                        "turn": self._log_turn,
                        "step": self._log_step,
                        "callId": tc.id,
                        "name": tc.name,
                        "input": tc.input,
                    }
                    if tc.parse_error:
                        tool_call_data["parseError"] = tc.parse_error
                    self._log_append("tool/call", tool_call_data)
                    self._log_tool_meta[tc.id] = {
                        "name": tc.name,
                        "turn": self._log_turn,
                        "step": self._log_step,
                        "status": "pending",
                        "result": None,
                        "logged": False,
                    }

                # Calls whose arguments the provider couldn't parse skip approval
                # and execution — they get an error result that tells the model
                # to re-issue the call with valid JSON (usually escaping quotes).
                malformed: dict[str, str] = {
                    tc.id: tc.parse_error for tc in tool_calls if tc.parse_error
                }
                for cid, err in malformed.items():
                    meta = self._log_tool_meta[cid]
                    meta["status"] = "malformed"
                    meta["result"] = ToolResult(
                        tool_call_id=cid,
                        name=meta["name"],
                        content=(
                            f"[Error: could not parse tool arguments as JSON: "
                            f"{err}. Re-issue the call with valid JSON — check "
                            f"that quotes and newlines inside string values are "
                            f"properly escaped.]"
                        ),
                        is_error=True,
                    )

                # Assess risk for all tool calls
                approvals: list[tuple[Any, str]] = []
                for tc in tool_calls:
                    if tc.id in malformed:
                        continue
                    tool = self.tools._tools.get(tc.name)
                    risk = getattr(tool, 'risk_level', 'safe')
                    if tc.name == "bash" and hasattr(tool, 'assess_command_risk'):
                        risk = tool.assess_command_risk(
                            tc.input.get("command", "")
                        )
                    # Sandbox escalation: `sandbox_permissions` + `justification`
                    # raise this call's risk to dangerous so the approval prompt
                    # fires BEFORE anything runs. A malformed pairing is rejected
                    # here with an error result (no approval, no execution).
                    if (
                        tc.input.get("sandbox_permissions") is not None
                        or tc.input.get("justification") is not None
                    ):
                        err = validate_escalation_args(
                            tc.input.get("sandbox_permissions"),
                            tc.input.get("justification"),
                        )
                        if err is not None:
                            malformed[tc.id] = err
                            meta = self._log_tool_meta[tc.id]
                            meta["status"] = "malformed"
                            meta["result"] = ToolResult(
                                tool_call_id=tc.id,
                                name=tc.name,
                                content=err,
                                is_error=True,
                            )
                            continue
                        risk = "dangerous"
                    approvals.append((tc, risk))

                # Collect permissions sequentially (UI may block on each).
                # Malformed calls never prompt — no valid params to show.
                denied: set[str] = set()
                # Hook feedback (PreToolUse/PostToolUse additionalContext)
                # collected during this step's tool execution, injected after the
                # tool results so the model sees it on the next request.
                hook_feedback: list[str] = []
                if callbacks:
                    self._log_stage = STAGE_APPROVAL
                    for tc, risk in approvals:
                        if tc.id in malformed:
                            continue
                        if not await callbacks.on_tool_start(
                            tc.name, tc.input, tc.id, risk
                        ):
                            denied.add(tc.id)
                            meta = self._log_tool_meta[tc.id]
                            meta["status"] = "denied"
                            meta["result"] = ToolResult(
                                tool_call_id=tc.id,
                                name=tc.name,
                                content=TOOL_DENIED_TEXT,
                                is_error=True,
                            )
                        else:
                            self._log_tool_meta[tc.id]["status"] = "approved"

                # Execute approved tools in parallel, denied tools get error
                # results. Each call fires its own on_tool_end the moment it
                # settles — crucially, even when the turn is cancelled or times
                # out mid-flight. If we only notified after gather() returned, a
                # cancellation raised inside a still-running tool would skip that
                # loop entirely and the UI's tool card would hang in "running"
                # forever. Emitting a terminal result here guarantees the card
                # always resolves. (The underlying subprocess may keep running
                # in its executor thread — orphan cleanup is a separate concern.)
                self._log_stage = STAGE_TOOL_EXECUTING

                async def _execute_one(tc: ToolCall):
                    meta = self._log_tool_meta[tc.id]
                    if tc.id in malformed or tc.id in denied:
                        result = meta["result"]
                    else:
                        # PreToolUse hook: a block denies THIS call (mirrors a
                        # user deny, but the reason comes from the hook and is fed
                        # back to the model). Feedback is collected for injection.
                        if self.hooks is not None and self.hooks.has_event("PreToolUse"):
                            hr = await self.hooks.run_event(
                                "PreToolUse", tool_name=tc.name, tool_input=tc.input,
                            )
                            if hr.feedback:
                                hook_feedback.extend(hr.feedback)
                            if hr.blocked:
                                meta["status"] = "hook_blocked"
                                meta["result"] = ToolResult(
                                    tool_call_id=tc.id,
                                    name=tc.name,
                                    content=hr.reason or "[Tool blocked by hook]",
                                    is_error=True,
                                )
                                result = meta["result"]
                                if callbacks is not None:
                                    await callbacks.on_tool_end(tc.id, result)
                                return result
                        if callbacks is not None:
                            selected = await callbacks.get_tool_selection(tc.id)
                            if selected is not None:
                                tc.input["_selected"] = selected
                            # ask_user_question pauses here for the frontend's
                            # answer (mirrors approval + edit-selection, both of
                            # which also run in the loop rather than the tool).
                            if tc.name == "ask_user_question":
                                questions = tc.input.get("questions") or []
                                if questions:
                                    tc.input["_answer"] = await callbacks.ask_question(
                                        questions, tc.id
                                    )
                        try:
                            result = await self.tools.call(tc.name, tc.input, tc.id)
                        except asyncio.CancelledError:
                            # Turn cancelled/timed out while this tool was still
                            # running. Emit a terminal result so the UI leaves
                            # "running", then re-raise to preserve cancellation.
                            if callbacks is not None:
                                await callbacks.on_tool_end(tc.id, ToolResult(
                                    tool_call_id=tc.id,
                                    name=tc.name,
                                    content="[Tool interrupted: the turn was cancelled or timed out before it finished]",
                                    is_error=True,
                                ))
                            raise
                        meta["status"] = "executed"
                        meta["result"] = result
                        # PostToolUse hook: fires for executed tools only. Too
                        # late to block; only feedback is honored.
                        if self.hooks is not None and self.hooks.has_event("PostToolUse"):
                            hr = await self.hooks.run_event(
                                "PostToolUse",
                                tool_name=tc.name,
                                tool_input=tc.input,
                                tool_response=result.content,
                            )
                            if hr.feedback:
                                hook_feedback.extend(hr.feedback)
                    if callbacks is not None:
                        await callbacks.on_tool_end(tc.id, result)
                    return result

                # on_tool_end fires inside _execute_one (incl. on cancellation),
                # so there is no separate notify loop after gather.
                results = await asyncio.gather(*[_execute_one(tc) for tc in tool_calls])

                # Append tool results to messages
                appended: list[dict[str, Any]] = []
                for tc, tr in zip(tool_calls, results):
                    result_msg = ToolResultMessage(
                        tool_call_id=tc.id,
                        content=tr.content,
                        name=tc.name,
                    )
                    api_msg = self.provider.tool_result_to_api(result_msg)
                    messages.append(api_msg)
                    appended.append(api_msg)
                    meta = self._log_tool_meta.get(tc.id)
                    status = meta["status"] if meta else "executed"
                    self._log_tool_result(
                        tc.id,
                        api_msg,
                        error=_tool_result_error(status, bool(tr.is_error)),
                    )

                # Account for the tool results we just added (they'll be part of
                # the next request's input but aren't in this response's usage).
                ctx_tokens += estimate_tokens(appended)

                # Hook feedback (PostToolUse/PreToolUse additionalContext):
                # inject as synthetic user messages after the tool results so the
                # model sees it on the next request. Model-visible ⟺ logged.
                for fb in hook_feedback:
                    fb_msg: dict[str, Any] = {"role": "user", "content": fb}
                    messages.append(fb_msg)
                    ctx_tokens += estimate_tokens([fb_msg])
                    self._log_append(
                        "user/message",
                        {"message": fb_msg, "source": "hook"},
                        surface_op=APPEND,
                    )

                # Doom-loop guard: observe this step's calls — including denied
                # and malformed, since hammering a rejected call is exactly the
                # loop to break — and, when a threshold is hit, inject the
                # strongest reminder as a synthetic user message after the tool
                # results. Advisory only; the loop never blocks on it.
                reminder: str | None = None
                for tc in tool_calls:
                    r = self._repeat_reminder(tc.name, tc.input)
                    if r:
                        reminder = r
                if reminder:
                    reminder_msg: dict[str, Any] = {
                        "role": "user", "content": reminder,
                    }
                    messages.append(reminder_msg)
                    ctx_tokens += estimate_tokens([reminder_msg])
                    # Model-visible ⟺ logged: record the injected message so
                    # derive_messages() reconstructs the exact request.
                    self._log_append(
                        "user/message",
                        {"message": reminder_msg, "source": "loop-guard"},
                        surface_op=APPEND,
                    )

                tool_calls_made += len(tool_calls)
                self._log_append(
                    "step/end", {"turn": self._log_turn, "step": step}
                )
                continue

            # Output budget exhausted ("max_tokens") or an unexpected stop.
            # Thinking models charge reasoning tokens against the output
            # budget, so this can arrive with NO visible reply at all — the
            # reasoning consumed everything and the completion was cut off
            # mid-thought. Surface that honestly instead of leaving the UI
            # with a silent empty bubble; the marker also lands in history so
            # the model knows what happened if the user continues.
            text = response.text or ""
            if response.stop_reason == "max_tokens":
                budget = self.provider.max_tokens()
                if text:
                    text += (
                        f"\n\n[Output truncated at the {budget}-token limit — "
                        f"the reply may be incomplete.]"
                    )
                else:
                    text = (
                        f"[Output truncated: reasoning consumed the entire "
                        f"{budget}-token output budget before producing a "
                        f"reply. Resend the message to retry.]"
                    )
            elif not text:
                text = (
                    "[The model stopped without producing any reply. "
                    "Resend the message to retry.]"
                )
            assistant = AssistantMessage(text=text)
            api_msg = self.provider.assistant_message_to_api(assistant)
            messages.append(api_msg)
            self._log_assistant(
                api_msg, usage=usage, reasoning=reasoning, timing=step_timing
            )
            self._log_append("step/end", {"turn": self._log_turn, "step": step})
            if end_reason is not None and response.stop_reason == "max_tokens":
                end_reason["kind"] = "max-tokens"
            cache_usage = None
            if input_tokens_total:
                cache_usage = {
                    "input_tokens": input_tokens_total,
                    "cache_read": cache_read_total,
                    "cache_write": cache_write_total,
                }
            return AgentResult(
                text=text,
                history=messages[1:],
                tool_calls_made=tool_calls_made,
                cache_usage=cache_usage,
                ttft_ms=ttft_ms,
                gen_ms=gen_ms_total,
                out_tokens=out_tokens_total,
            )

        if end_reason is not None:
            end_reason["kind"] = "max-turns"
        # The safety stop must ALSO land in the log (Model-visible ⟺ logged):
        # without an assistant message here, a max-turns turn would close with
        # turn/end{max-turns} but leave the surface ending on tool results — an
        # orphaned transcript the next turn can't replay.
        text = "[Agent loop exceeded maximum turns]"
        assistant = AssistantMessage(text=text)
        api_msg = self.provider.assistant_message_to_api(assistant)
        messages.append(api_msg)
        self._log_assistant(api_msg)
        return AgentResult(
            text=text,
            history=messages[1:],
            tool_calls_made=tool_calls_made,
            ttft_ms=ttft_ms,
            gen_ms=gen_ms_total,
            out_tokens=out_tokens_total,
        )
