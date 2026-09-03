"""JSON-RPC 2.0 server over stdin/stdout for CluxMate desktop.

Architecture:
- stdin thread: reads stdin lines into a queue.Queue
- Main thread: polls queue, dispatches to agent thread or tool decisions
- Agent thread: runs LLM + tool execution in its own asyncio event loop
- Stdout: threading.Lock protects all writes — any thread can write
"""

import asyncio
import atexit
import concurrent.futures
import json
import os
import queue as tqueue
import re
import sys
import threading
import time
import traceback
from typing import Any

from cluxmate.core.agent import AgentCallbacks, AgentLoop, NETWORK_FALLBACK_TEXT, ToolDecision
from cluxmate.core.builder import AgentBuilder
from cluxmate.core.checkpoints import CheckpointManager
from cluxmate.core.grants import GrantStore
from cluxmate.core.read_denies import ReadDenyStore
from cluxmate.core.hooks import HookManager
from cluxmate.core.permissions import PermissionPolicy
from cluxmate.core.session_log import (
    SessionHeader,
    SessionLog,
    reconstruct_turn_contexts,
)
from cluxmate.core.session_log_store import (
    IncrementalPersister,
    SessionLogCorruptionError,
    SessionLogStore,
    SessionNotFoundError,
    orphaned_subagent_ids,
    replay_subagents,
)
from cluxmate.tools.base import ToolResult

_SURROGATE_RE = re.compile(r'[\ud800-\udfff]')
_stdout_lock = threading.Lock()


def _write_dict(obj: dict[str, Any]):
    """Thread-safe stdout write."""
    data = json.dumps(obj, ensure_ascii=False)
    data = _SURROGATE_RE.sub('', data)
    with _stdout_lock:
        sys.stdout.buffer.write(data.encode('utf-8') + b'\n')
        sys.stdout.buffer.flush()


class _CancelledError(Exception):
    pass


# Safe (read-only) tools normally stay hidden in the root UI — they auto-approve
# and emit no tool_start, so no card renders. But long-running *network* tools
# (web search / fetch) can occupy the turn for minutes with zero visible
# feedback, which reads as a freeze. These are surfaced as running cards anyway:
# still auto-approved (no permission prompt), just visible so the user can see
# "searching / fetching" instead of an empty spinner. High-frequency local reads
# (read_file, grep, list_dir) stay hidden to avoid card spam.
_VISIBLE_SAFE_TOOLS = {"web_search", "web_fetch", "ask_user_question"}


def _quiet_closed_loop_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    """Swallow the benign 'Event loop is closed' RuntimeError from httpx teardown.

    On Windows the ProactorEventLoop's TLS transport schedules its
    connection-lost callback via loop.call_soon during aclose(). If that
    callback (or an abandoned streaming response finalized by the GC) fires
    after the per-turn loop is closed, call_soon hits _check_closed() and
    raises RuntimeError('Event loop is closed'). It surfaces as an orphaned
    Task whose exception is never retrieved, spamming stderr — but the pool is
    already gone and the turn already returned, so it is purely cosmetic. Drop
    just that one error; delegate everything else to the default handler so real
    failures still surface.
    """
    exc = context.get("exception")
    if isinstance(exc, RuntimeError) and "Event loop is closed" in str(exc):
        return
    loop.default_exception_handler(context)


_TITLE_PROMPT = (
    "Write a short, specific title (3-6 words, no quotes, no trailing period) "
    "for a coding-assistant conversation that starts with the exchange below. "
    "Capture the concrete task or question. Output only the title."
)
_TITLE_MAX_LEN = 50

# How long a turn will wait for the background MCP loader before proceeding
# without MCP tools. The loader is internally bounded — each server's handshake
# is capped at mcp._HANDSHAKE_TIMEOUT_S (5s) + 1s, run in parallel — so a
# healthy or fail-soft load settles well within this. The cap only guards a
# pathological hang (it must exceed the loader's own bound so a merely-slow
# server isn't abandoned mid-handshake). Only the FIRST turn after initialize
# can pay this; once loaded, the event is already set and the wait is instant.
_MCP_READY_WAIT_S = 8.0


async def _generate_title(provider: Any, user_text: str, assistant_text: str) -> str | None:
    """One short, non-streaming LLM call to name a session. Returns None on any
    failure — titling is best-effort and must never break the turn."""
    convo = f"User: {user_text[:1500]}\n\nAssistant: {(assistant_text or '')[:1500]}"
    try:
        # No on_delta: this is a side call, not part of the visible reply stream.
        resp = await provider.chat(
            [
                {"role": "system", "content": _TITLE_PROMPT},
                {"role": "user", "content": convo},
            ],
            [],  # no tools
        )
    except Exception:
        return None
    title = (resp.text or "").strip().strip('"').strip("'").replace("\n", " ").strip()
    if not title:
        return None
    if len(title) > _TITLE_MAX_LEN:
        title = title[:_TITLE_MAX_LEN].rstrip() + "..."
    return title


class JsonRpcCallbacks(AgentCallbacks):

    def __init__(self, policy: "PermissionPolicy"):
        super().__init__()
        self._policy = policy
        self._tool_events: dict[str, threading.Event] = {}
        self._tool_decisions: dict[str, bool] = {}
        self._tool_decision_kind: dict[str, str] = {}
        self._tool_selections: dict[str, list[int]] = {}
        self._pending_names: dict[str, str] = {}
        self._pending_risk: dict[str, str] = {}
        self._pending_categories: dict[str, frozenset[str]] = {}
        self._question_events: dict[str, threading.Event] = {}
        self._question_answers: dict[str, dict[str, Any]] = {}
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        for evt in self._tool_events.values():
            evt.set()
        for evt in self._question_events.values():
            evt.set()

    def resolve_tool(self, call_id: str, approved: bool, always: bool = False, selected: list[int] | None = None):
        if always and approved:
            name = self._pending_names.get(call_id)
            if name:
                # Persist into the tier the call actually ran at: a dangerous
                # call goes to the dangerous list (bash category-scoped as
                # `bash:<category>`), a write call to the write list. Escalation
                # and critical never reach here (the UI hides "always" for them).
                risk = self._pending_risk.get(call_id)
                if risk == "dangerous":
                    if name == "bash":
                        # Category-scoped: persist one `bash:<category>` grant per
                        # matched destructive category (never a bare "bash").
                        for c in (self._pending_categories.get(call_id) or frozenset()):
                            self._policy.add_always_allow_dangerous(f"bash:{c}")
                    else:
                        self._policy.add_always_allow_dangerous(name)
                elif risk == "write":
                    self._policy.add_always_allow(name)
        self._tool_decisions[call_id] = approved
        # Record HOW it was settled for the audit trail: "always" when the user
        # clicked "总是允许", else "user" (approved) or "denied".
        self._tool_decision_kind[call_id] = (
            "always" if (always and approved) else ("user" if approved else "denied")
        )
        if selected is not None:
            self._tool_selections[call_id] = selected
        evt = self._tool_events.get(call_id)
        if evt is not None:
            evt.set()

    async def get_tool_selection(self, call_id: str) -> list[int] | None:
        return self._tool_selections.pop(call_id, None)

    def resolve_question(self, call_id: str, answers: list[dict[str, Any]]) -> None:
        """Wake a blocked ``ask_question`` with the user's answers."""
        self._question_answers[call_id] = {"answers": answers}
        evt = self._question_events.get(call_id)
        if evt is not None:
            evt.set()

    async def ask_question(
        self, questions: list[dict[str, Any]], call_id: str
    ) -> dict[str, Any] | None:
        """Emit a dedicated ``question`` event and block until the user answers.

        Mirrors the approval gate: the wait runs on an executor thread so the
        event loop stays free for other tools / streamed text / cancel. Returns
        ``{"answers": [...]}``, or None when there is nothing to ask (an empty
        batch), and raises ``_CancelledError`` when the turn is cancelled.
        """
        if self._cancelled:
            raise _CancelledError()
        if not questions:
            return None
        _write_dict({
            "jsonrpc": "2.0", "method": "chat/stream",
            "params": {
                "type": "question", "call_id": call_id, "questions": questions,
            },
        })
        evt = threading.Event()
        self._question_events[call_id] = evt
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, evt.wait)
        self._question_events.pop(call_id, None)
        if self._cancelled:
            raise _CancelledError()
        return self._question_answers.pop(call_id, None)

    async def on_tool_start(
        self,
        name: str,
        params: dict[str, Any],
        call_id: str,
        risk_level: str,
        categories: frozenset[str] = frozenset(),
    ) -> ToolDecision:
        if self._cancelled:
            raise _CancelledError()
        # Safe (read-only) tools never prompt. Most stay hidden in the root UI
        # (no tool_start emitted); the network tools in _VISIBLE_SAFE_TOOLS are
        # the exception — emit a running card so a multi-minute search/fetch
        # isn't an invisible freeze, but still auto-approve without a prompt.
        if risk_level == "safe":
            if name in _VISIBLE_SAFE_TOOLS:
                _write_dict({
                    "jsonrpc": "2.0", "method": "chat/stream",
                    "params": {
                        "type": "tool_start", "call_id": call_id,
                        "name": name, "input": params, "risk_level": risk_level,
                        "auto_approved": True, "visible": True,
                    },
                })
            return ToolDecision(True, "auto")

        escalated = params.get("sandbox_permissions") == "danger-full-access"
        auto = self._policy.is_auto_approved(
            name, risk_level, escalated=escalated, categories=categories
        )
        always_allowable = self._policy.is_always_allowable(
            name, risk_level, escalated=escalated
        )

        # Emit tool_start so the UI renders the tool card. auto_approved tells it
        # whether a permission prompt follows: when true the card goes straight to
        # "running" with no approve/deny buttons. always_allowable tells it whether
        # to render the "总是允许" button (false for escalation / other dangerous).
        # categories lets the UI show "总是允许 rm" instead of a coarse "bash".
        _write_dict({
            "jsonrpc": "2.0", "method": "chat/stream",
            "params": {
                "type": "tool_start", "call_id": call_id,
                "name": name, "input": params, "risk_level": risk_level,
                "auto_approved": auto, "always_allowable": always_allowable,
                "categories": sorted(categories),
            },
        })

        if auto:
            return ToolDecision(True, "auto")

        self._pending_names[call_id] = name
        self._pending_risk[call_id] = risk_level
        self._pending_categories[call_id] = categories
        evt = threading.Event()
        self._tool_events[call_id] = evt

        # Wait indefinitely on a thread so the event loop stays free for
        # other tool_start calls, text streaming, and chat/cancel.  Claude
        # Code has no approval timeout — the user can take as long as they
        # need to review a tool call before approving or denying it.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, evt.wait)
        self._tool_events.pop(call_id, None)
        self._pending_names.pop(call_id, None)
        self._pending_risk.pop(call_id, None)
        self._pending_categories.pop(call_id, None)
        if self._cancelled:
            raise _CancelledError()
        approved = self._tool_decisions.pop(call_id, False)
        kind = self._tool_decision_kind.pop(call_id, "denied")
        return ToolDecision(approved, kind)

    async def on_tool_end(self, call_id: str, result: ToolResult) -> None:
        _write_dict({
            "jsonrpc": "2.0", "method": "chat/stream",
            "params": {
                "type": "tool_result", "call_id": call_id,
                "output": result.content, "is_error": result.is_error,
            },
        })

    async def on_text_delta(self, chunk: str) -> None:
        # Called per streamed text segment. Must not block — _write_dict only
        # takes the stdout lock; no awaits/sleeps here so the provider's stream
        # keeps draining and other events (tool_start) stay responsive.
        _write_dict({
            "jsonrpc": "2.0", "method": "chat/stream",
            "params": {"type": "text_delta", "content": chunk},
        })

    async def on_thinking_delta(self, chunk: str) -> None:
        _write_dict({
            "jsonrpc": "2.0", "method": "chat/stream",
            "params": {"type": "thinking", "content": chunk},
        })

    async def on_text_restart(self) -> None:
        _write_dict({
            "jsonrpc": "2.0", "method": "chat/stream",
            "params": {"type": "text_restart"},
        })

    # ── subagent tracker interface ─────────────────────────────
    # TaskTool / SkillTool reach these via builder._tracker (set each turn by
    # _handle_chat_send). scoped() hands each subagent its own callbacks so the
    # child's tool/text events are tagged with the child agent_id and routed to
    # the correct tree node; the lifecycle emitters drive the tree itself.

    def scoped(self, agent_id: str, auto_approve: bool = True) -> "ScopedCallbacks":
        return ScopedCallbacks(self, agent_id)

    async def on_agent_start(
        self, agent_id: str, parent_id: str, subagent_type: str,
        description: str, depth: int, prompt: str = "",
    ) -> None:
        _write_dict({
            "jsonrpc": "2.0", "method": "chat/stream",
            "params": {
                "type": "agent_start", "agent_id": agent_id,
                "parent_id": parent_id, "subagent_type": subagent_type,
                "description": description, "depth": depth, "prompt": prompt,
            },
        })

    async def on_agent_end(
        self,
        agent_id: str,
        status: str,
        result: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        _write_dict({
            "jsonrpc": "2.0", "method": "chat/stream",
            "params": {
                "type": "agent_end", "agent_id": agent_id,
                "status": status, "result": result,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        })

    async def on_skill_used(
        self, name: str, slug: str, source: str, trigger: str,
        agent_id: str = "root",
    ) -> None:
        _write_dict({
            "jsonrpc": "2.0", "method": "chat/stream",
            "params": {
                "type": "skill_used", "name": name, "slug": slug,
                "source": source, "trigger": trigger, "agent_id": agent_id,
            },
        })


class ScopedCallbacks(AgentCallbacks):
    """Per-subagent callbacks that tag every event with the child's agent_id.

    Delegates cancellation/selection state to the shared JsonRpcCallbacks but
    emits its own tool/text events so the desktop routes them to this subagent's
    tree node (events whose agent_id != "root" attach to a node, not the root
    message body). Subagents run autonomously: their tool calls stream for the
    tree and auto-approve — they never raise a permission prompt.
    """

    def __init__(self, shared: "JsonRpcCallbacks", agent_id: str):
        super().__init__()
        self._shared = shared
        self._agent_id = agent_id

    async def on_tool_start(
        self,
        name: str,
        params: dict[str, Any],
        call_id: str,
        risk_level: str,
        categories: frozenset[str] = frozenset(),
    ) -> ToolDecision:
        # Honor a turn-level cancel even inside a subagent.
        if self._shared._cancelled:
            raise _CancelledError()

        # Subagents run autonomously — they never raise an interactive approval
        # prompt (that would block the parent's `task` call against its 180s
        # timeout). But "spawn a subagent" must not become a back door around
        # dangerous-command gating: outside yolo, a subagent's dangerous call is
        # DENIED here (no prompt), and the loop feeds a denied result back so the
        # subagent picks another path. safe + write always run (write autonomy is
        # intentional — the parent already had to reach this mode to spawn at all).
        denied = risk_level in ("dangerous", "critical") and self._shared._policy.mode != "yolo"

        # Emit for EVERY tool (incl. safe/read-only) so the subagent tree is
        # complete. auto_approved reflects whether it will actually run.
        _write_dict({
            "jsonrpc": "2.0", "method": "chat/stream",
            "params": {
                "type": "tool_start", "call_id": call_id,
                "name": name, "input": params, "risk_level": risk_level,
                "auto_approved": not denied, "agent_id": self._agent_id,
            },
        })
        return ToolDecision(not denied, "denied" if denied else "auto")

    async def on_tool_end(self, call_id: str, result: ToolResult) -> None:
        _write_dict({
            "jsonrpc": "2.0", "method": "chat/stream",
            "params": {
                "type": "tool_result", "call_id": call_id,
                "output": result.content, "is_error": result.is_error,
                "agent_id": self._agent_id,
            },
        })

    async def on_text_delta(self, chunk: str) -> None:
        _write_dict({
            "jsonrpc": "2.0", "method": "chat/stream",
            "params": {
                "type": "text_delta", "content": chunk,
                "agent_id": self._agent_id,
            },
        })

    async def on_thinking_delta(self, chunk: str) -> None:
        _write_dict({
            "jsonrpc": "2.0", "method": "chat/stream",
            "params": {
                "type": "thinking", "content": chunk,
                "agent_id": self._agent_id,
            },
        })

    async def on_text_restart(self) -> None:
        _write_dict({
            "jsonrpc": "2.0", "method": "chat/stream",
            "params": {"type": "text_restart", "agent_id": self._agent_id},
        })

    async def get_tool_selection(self, call_id: str) -> list[int] | None:
        return await self._shared.get_tool_selection(call_id)


class JsonRpcServer:

    def __init__(self):
        self._agent: AgentLoop | None = None
        self._builder: AgentBuilder | None = None
        self._cwd = os.getcwd()
        self._session_id = ""
        # SessionStart hook feedback — prepended to the FIRST turn's injections
        # (one-shot, cleared by _handle_chat_send). Empty when none / not blocked.
        self._session_start_feedback: list[str] = []
        # Shadow-git checkpoints for undo/rewind + per-turn diffs. Built at
        # initialize; None (or ensure_init False) means the feature is disabled.
        self._checkpoints: CheckpointManager | None = None
        self._callbacks: JsonRpcCallbacks | None = None
        # The in-flight turn's asyncio loop + task. Registered by the turn thread
        # right before it runs, so a Stop (chat/cancel) or a superseding send can
        # interrupt an in-flight stream from the dispatch thread. Without this,
        # Stop only flips a flag the loop checks at approval boundaries, so an
        # in-flight stream keeps running and overlaps the next turn — racing on
        # the shared agent/provider/session-log.
        self._turn_loop: asyncio.AbstractEventLoop | None = None
        self._turn_task: asyncio.Task | None = None
        # Bumped on every initialize. A background MCP loader captures the gen at
        # spawn and only swaps in its rebuilt agent if the gen still matches — a
        # re-initialize (new cwd/model) supersedes an in-flight load so its stale
        # agent never clobbers the current one.
        self._init_gen = 0
        # Set by the background MCP loader when it finishes (success, failure, or
        # stale — always). A turn waits on this (bounded) before capturing the
        # agent so the first message gets MCP tools. Replaced per initialize;
        # pre-set so a turn on a not-yet-initialized server never blocks.
        self._mcp_ready = threading.Event()
        self._mcp_ready.set()
        # Per-project tool-approval policy. Bound to the default cwd here; rebuilt
        # against the real workspace in initialize() once the desktop sends it, so
        # "accept edits" is scoped to <cwd>/.cluxmate/permissions.json and does
        # not follow the user to a different project.
        self._policy = PermissionPolicy(self._cwd)
        # Writable-folder grants (sandbox-grants.json). Lazily constructed at
        # first use — a read-only home or a headless process shouldn't force it.
        self._grants: GrantStore | None = None
        # Read-denylist (forbid-read.json). Same lazy-construction rationale.
        self._read_denies: ReadDenyStore | None = None
        # JSONL event-log persistence for the active session (Python owns history
        # now — the desktop no longer writes <id>.json). Loaded/created at
        # initialize; flushed after each turn; truncated on undo.
        self._log_store = SessionLogStore()
        self._session_log: SessionLog | None = None
        # Current config entry id + reasoning effort the agent's provider was
        # built with. A chat/send can override either (per-message model switch +
        # reasoning-level selection); these track the live value so we only
        # rebuild/mutate when something actually changed.
        self._model_id: str = ""
        self._reasoning_effort: str | None = None
        # Incrementally persists the active session log as events are appended,
        # so a process kill mid-turn leaves the partial turn on disk.
        self._persister: IncrementalPersister | None = None
        self._line_queue: tqueue.Queue[str | None] = tqueue.Queue()
        atexit.register(self._shutdown_mcp)
        atexit.register(self._shutdown_lsp)

    def run(self):
        # Daemon thread: read stdin into queue
        threading.Thread(target=self._read_stdin, daemon=True).start()

        while True:
            line = self._line_queue.get()
            if line is None:
                break

            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                _write_dict({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue

            req_id = request.get("id")
            method = request.get("method", "")
            params = request.get("params", {})

            try:
                self._dispatch(req_id, method, params)
            except Exception as e:
                traceback.print_exc(file=sys.stderr)
                _write_dict({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": str(e)}})

        # stdin closed — the desktop shut the bridge down (clean shutdown, not
        # a crash). SessionEnd hooks get one last run; their output is discarded
        # (nothing is left to block or feed).
        self._fire_session_end("exit")

    def _read_stdin(self):
        for line in sys.stdin:
            self._line_queue.put(line.strip())
        self._line_queue.put(None)

    def _dispatch(self, req_id: Any, method: str, params: dict[str, Any]):
        if method == "initialize":
            self._handle_initialize(req_id, params)
        elif method == "chat/send":
            self._handle_chat_send(req_id, params)
        elif method == "chat/cancel":
            self._cancel_chat()
            if req_id is not None:
                _write_dict({"jsonrpc": "2.0", "id": req_id, "result": {"status": "cancelled"}})
        elif method == "tool/approve":
            always = bool(params.get("always", False))
            selected = params.get("selected")
            # Accept both list[int] and list passed as-is from JSON
            if isinstance(selected, list):
                selected = [int(x) for x in selected]
            self._tool_decision(params["call_id"], True, always, selected)
            if req_id is not None:
                _write_dict({"jsonrpc": "2.0", "id": req_id, "result": {"call_id": params["call_id"], "approved": True, "always": always}})
        elif method == "tool/deny":
            self._tool_decision(params["call_id"], False)
            if req_id is not None:
                _write_dict({"jsonrpc": "2.0", "id": req_id, "result": {"call_id": params["call_id"], "approved": False}})
        elif method == "question/answer":
            answers = params.get("answers", [])
            if not isinstance(answers, list):
                answers = []
            self._question_decision(params["call_id"], answers)
            if req_id is not None:
                _write_dict({"jsonrpc": "2.0", "id": req_id, "result": {"call_id": params["call_id"], "answered": True}})
        elif method == "checkpoint/list":
            result = self._checkpoints.list(self._session_id) if self._checkpoints else []
            _write_dict({"jsonrpc": "2.0", "id": req_id, "result": {"checkpoints": result}})
        elif method == "checkpoint/diff":
            result = self._checkpoints.diff(params["checkpoint_id"]) if self._checkpoints else []
            _write_dict({"jsonrpc": "2.0", "id": req_id, "result": {"files": result}})
        elif method == "checkpoint/restore":
            result = (
                self._checkpoints.restore(params["checkpoint_id"], self._session_id)
                if self._checkpoints else {"restored": [], "deleted": []}
            )
            _write_dict({"jsonrpc": "2.0", "id": req_id, "result": result})
        elif method in ("mcp/list", "mcp/status"):
            servers = self._builder.mcp_status() if self._builder else []
            _write_dict({"jsonrpc": "2.0", "id": req_id, "result": {"servers": servers}})
        elif method in ("mcp/shutdown", "mcp:shutdown"):
            self._shutdown_mcp()
            if req_id is not None:
                _write_dict({"jsonrpc": "2.0", "id": req_id, "result": {"status": "ok"}})
        elif method in ("permissions/get", "permissions:get"):
            _write_dict({"jsonrpc": "2.0", "id": req_id, "result": self._policy.snapshot()})
        elif method in ("hooks/get", "hooks:get"):
            hooks = self._builder._hooks_manager() if self._builder else None
            _write_dict({"jsonrpc": "2.0", "id": req_id, "result": {
                "hooks": hooks.list_hooks() if hooks is not None else [],
            }})
        elif method in ("hooks/reload", "hooks:reload"):
            hooks_list = self._builder.reload_hooks() if self._builder else []
            _write_dict({"jsonrpc": "2.0", "id": req_id, "result": {"hooks": hooks_list}})
        elif method in ("hooks/notify", "hooks:notify"):
            # Manual Notification trigger (fire-and-forget): runs the
            # Notification hooks on a background thread with ``message`` in the
            # payload. Output is discarded — see _fire_notification.
            message = params.get("message")
            if isinstance(message, str) and message.strip():
                self._fire_notification(message.strip())
            if req_id is not None:
                _write_dict({"jsonrpc": "2.0", "id": req_id, "result": {"status": "scheduled"}})
        elif method in ("permissions/update", "permissions:update"):
            if "accept_edits" in params:
                self._policy.set_accept_edits(bool(params["accept_edits"]))
            if req_id is not None:
                _write_dict({"jsonrpc": "2.0", "id": req_id, "result": self._policy.snapshot()})
        elif method in ("sandbox/grants", "sandbox/grants/get", "sandbox:grants"):
            _write_dict({"jsonrpc": "2.0", "id": req_id, "result": self._grants_snapshot()})
        elif method in ("sandbox/grants/set", "sandbox:grants:set"):
            result = self._set_grants(params.get("paths", []))
            _write_dict({"jsonrpc": "2.0", "id": req_id, "result": result})
        elif method in ("sandbox/forbid_read", "sandbox/forbid_read/get", "sandbox:forbid_read"):
            _write_dict({"jsonrpc": "2.0", "id": req_id, "result": self._forbid_read_snapshot()})
        elif method in ("sandbox/forbid_read/set", "sandbox:forbid_read:set"):
            result = self._set_forbid_read(params)
            _write_dict({"jsonrpc": "2.0", "id": req_id, "result": result})
        elif method in ("ssrf/config", "ssrf/config/get", "ssrf:config"):
            _write_dict({"jsonrpc": "2.0", "id": req_id, "result": self._ssrf_snapshot()})
        elif method in ("ssrf/config/set", "ssrf:config:set"):
            result = self._set_ssrf_config(params)
            _write_dict({"jsonrpc": "2.0", "id": req_id, "result": result})
        elif method in ("egress/config", "egress/config/get", "egress:config"):
            _write_dict({"jsonrpc": "2.0", "id": req_id, "result": self._egress_snapshot()})
        elif method in ("egress/config/set", "egress:config:set"):
            result = self._set_egress_config(params)
            _write_dict({"jsonrpc": "2.0", "id": req_id, "result": result})
        elif method in ("chat/set_mode", "chat:set_mode"):
            self._set_mode(params.get("mode", "default"))
            if req_id is not None:
                _write_dict({"jsonrpc": "2.0", "id": req_id, "result": self._policy.snapshot()})
        elif method in ("session/truncate", "session:truncate"):
            self._handle_truncate(
                params.get("session_id", self._session_id), int(params.get("seq", 0))
            )
            if req_id is not None:
                _write_dict({"jsonrpc": "2.0", "id": req_id, "result": {"status": "ok"}})
        elif method in ("session/replay", "session:replay"):
            sid = params.get("session_id") or self._session_id
            subagents = replay_subagents(self._log_store, sid) if sid else []
            _write_dict({"jsonrpc": "2.0", "id": req_id, "result": {"subagents": subagents}})
        elif method in ("session/context", "session:context"):
            sid = params.get("session_id") or self._session_id
            contexts = []
            if sid:
                try:
                    # inspect() (NOT load()): this is a read-only view, so it must
                    # not repair/append closers — an in-flight turn would otherwise
                    # be marked interrupted just by opening the context panel.
                    _header, events = self._log_store.inspect(sid)
                    contexts = reconstruct_turn_contexts(events)
                except (SessionNotFoundError, SessionLogCorruptionError):
                    contexts = []
            _write_dict({"jsonrpc": "2.0", "id": req_id, "result": {"turns": contexts}})
        else:
            raise ValueError(f"Unknown method: {method}")

    def _hook_observer(self, kind: str, data: dict[str, Any]) -> None:
        """Forward a hook lifecycle event to the desktop as a chat/stream event.

        ``kind`` is ``hook_start`` / ``hook_result``; ``data`` carries the hook
        event name, tool, command, and (for results) the outcome + timing.
        """
        _write_dict({
            "jsonrpc": "2.0", "method": "chat/stream",
            "params": {"type": kind, **data},
        })

    def _run_hooks_sync(self, hooks: "HookManager", event: str, extra: dict[str, Any] | None = None):
        """Run hook commands on a throwaway event loop (sync contexts).

        The dispatch thread has no running loop; each call spins one up just
        long enough for the hooks (bounded by their timeouts) and closes it.
        """
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(hooks.run_event(event, extra=extra))
        finally:
            loop.close()

    def _fire_session_end(self, reason: str) -> None:
        """Fire SessionEnd hooks; their output is discarded (nothing left to
        block or feed). Never raises — a broken hook must not break shutdown."""
        hooks = self._builder._hooks_manager() if self._builder else None
        if hooks is None or not hooks.has_event("SessionEnd"):
            return
        try:
            self._run_hooks_sync(hooks, "SessionEnd", {"reason": reason})
        except Exception:
            traceback.print_exc(file=sys.stderr)

    def _run_notification_hooks(self, hooks: "HookManager", message: str) -> None:
        """Fire-and-forget Notification run (fresh thread's loop).

        Output is discarded — Notification is a side-effect event.
        """
        try:
            self._run_hooks_sync(hooks, "Notification", {"message": message})
        except Exception:
            traceback.print_exc(file=sys.stderr)

    def _fire_notification(self, message: str) -> None:
        """Trigger Notification hooks on their own daemon thread.

        A slow Notification hook must never stall the turn thread or the
        dispatch thread; hook_start/hook_result still stream to the desktop
        (the observer writes are thread-safe).
        """
        hooks = self._builder._hooks_manager() if self._builder else None
        if hooks is None or not hooks.has_event("Notification"):
            return
        threading.Thread(
            target=self._run_notification_hooks, args=(hooks, message),
            daemon=True,
        ).start()

    def _handle_initialize(self, req_id: Any, params: dict[str, Any]):
        new_sid = params.get("session_id", "")
        # SessionEnd for the PREVIOUS session before tearing it down. Skipped
        # when the session id is unchanged — the desktop re-initializes the SAME
        # session after settings toggles kill/restart the bridge, which is a
        # continuation, not an end.
        if self._session_id and new_sid and self._session_id != new_sid:
            self._fire_session_end("other")
        # Re-init reuses one Python process; kill the previous builder's MCP
        # subprocesses before building a new one so they don't leak.
        self._shutdown_mcp()
        self._shutdown_lsp()
        self._shutdown_egress()
        self._cwd = params.get("cwd", os.getcwd())
        self._session_id = new_sid
        # Rebind the approval policy to this workspace's permissions.json so a
        # re-initialize onto a different cwd loads that project's policy.
        self._policy = PermissionPolicy(self._cwd)
        # Writable-folder grants are user-global (~/.cluxmate/sandbox-grants.json)
        # and survive re-init; load once and share with the builder.
        if getattr(self, "_grants", None) is None:
            self._grants = GrantStore()
        # Read-denylist is likewise user-global (~/.cluxmate/forbid-read.json).
        if getattr(self, "_read_denies", None) is None:
            self._read_denies = ReadDenyStore()
        # SSRF network-access config is likewise user-global (~/.cluxmate/ssrf.json).
        if getattr(self, "_ssrf_config", None) is None:
            from cluxmate.core.ssrf_config import SsrConfig
            self._ssrf_config = SsrConfig()
        # Network-egress config is likewise user-global (~/.cluxmate/egress.json).
        if getattr(self, "_egress_config", None) is None:
            from cluxmate.core.egress_config import EgressConfig
            self._egress_config = EgressConfig()
        model_id = params.get("model_id", "")
        # Development mode is per-session and not persisted; default unless the
        # desktop passes one on (re)initialize.
        mode = params.get("mode", "default")
        try:
            self._policy.set_mode(mode)
        except ValueError:
            mode = "default"
        provider, model_name, context_1m, entry = self._build_provider(model_id)
        from cluxmate.core.reasoning import default_for
        self._model_id = entry.get("id", "")
        self._reasoning_effort = default_for(entry)
        if self._reasoning_effort:
            provider.set_reasoning_effort(self._reasoning_effort)
        # Load the session's JSONL event log (or create a fresh one) — Python is
        # the sole writer of conversation history (D6). The log must exist before
        # builder.build(session_log=...) so the agent records every turn.
        # `log_created` tells SessionStart whether this is a fresh session or a
        # resume of a persisted one.
        self._session_log, log_created = self._load_or_create_log(self._session_id, entry)
        self._bind_persister()
        # Supersede any in-flight background MCP loader from a prior initialize.
        self._init_gen += 1
        gen = self._init_gen
        builder = AgentBuilder(self._cwd, provider)
        builder.with_default_tools()
        builder.with_grants(self._grants)
        builder.with_read_denies(self._read_denies)
        builder.with_ssrf(self._ssrf_config)
        builder.with_egress(self._egress_config)
        # Lifecycle hooks (settings.json). One manager per session so the payload
        # carries the session id; the builder caches it and children inherit it.
        # The observer streams hook_start/hook_result events to the desktop.
        hooks = HookManager(self._cwd)
        hooks.session_id = self._session_id
        hooks.set_observer(self._hook_observer)
        builder.with_hooks(hooks)
        # SessionStart hook: fires once per (process, session) pair, BEFORE the
        # session becomes usable. A block aborts initialization — the desktop
        # shows the reason. Feedback is prepended to the FIRST turn's injections
        # (one-shot, see _handle_chat_send).
        self._session_start_feedback = []
        if hooks.has_event("SessionStart"):
            hr = self._run_hooks_sync(
                hooks, "SessionStart",
                {"source": "resume" if not log_created else "startup"},
            )
            if hr.blocked:
                # Leave no half-built state: the previous agent/builder were
                # already shut down above; a stale agent must not serve the new
                # cwd or session.
                self._agent = None
                self._builder = None
                _write_dict({"jsonrpc": "2.0", "id": req_id, "error": {
                    "code": -32000,
                    "message": hr.reason or "[SessionStart hook blocked the session]",
                }})
                return
            self._session_start_feedback = hr.feedback
        builder.with_subagent_types(["general-purpose", "explore"])
        builder.with_mode(mode)
        if model_name:
            builder.with_model(model_name)
        builder.with_context_1m(context_1m)
        # Defer MCP: load() spawns npx/subprocesses and blocks the handshake for
        # seconds. Build without MCP tools now (fast return), then load them on a
        # background thread and hot-swap the agent when ready. See _load_mcp_async.
        builder.with_deferred_mcp()
        # Subagent logs are persisted through the same JSONL store as the parent.
        builder.with_log_store(self._log_store)
        self._agent = builder.build(session_log=self._session_log)
        self._builder = builder
        # Shadow-git checkpoints for this working directory. ensure_init is a
        # no-op when git is unavailable; the feature then silently disables.
        self._checkpoints = CheckpointManager(self._cwd)
        checkpoints_ok = self._checkpoints.ensure_init()
        _write_dict({"jsonrpc": "2.0", "id": req_id, "result": {
            "agent_version": "0.1.0",
            "tools": [t.definition() for t in builder._get_tools()],
            "checkpoints_enabled": checkpoints_ok,
            "permissions": self._policy.snapshot(),
        }})
        # Now warm MCP off the critical path. The desktop already has its
        # response; MCP tools become usable from the next turn after load done.
        # Fresh (unset) event for THIS generation's load — a turn that starts
        # before the load finishes waits on it (bounded) so the first message
        # still gets MCP tools. _load_mcp_async sets it in a finally.
        ready = threading.Event()
        self._mcp_ready = ready
        threading.Thread(
            target=self._load_mcp_async, args=(builder, gen, ready), daemon=True
        ).start()

    def _load_mcp_async(self, builder: "AgentBuilder", gen: int, ready: threading.Event):
        """Background: run the deferred MCP load, then hot-swap the agent.

        Runs on its own daemon thread so the initialize handshake isn't blocked
        by npx spawn + the tools/list handshake (~2s). When load finishes:
          - If a newer initialize has bumped _init_gen, this load is stale — the
            builder's MCP subprocesses would leak, so shut them down and bail.
          - Otherwise rebuild the agent (reuses the now-loaded, cached MCP
            manager, so no re-spawn) and atomically swap self._agent. A turn
            already in flight bound its own AgentLoop instance and is unaffected;
            MCP tools take effect from the next chat/send.

        `ready` is set in a finally regardless of outcome so a turn waiting on it
        (see _handle_chat_send) is always released — a failed or stale load must
        not leave the first message blocked for the full _MCP_READY_WAIT_S.
        """
        try:
            try:
                had_tools = builder.load_mcp()
            except Exception:
                traceback.print_exc(file=sys.stderr)
                return
            # Stale: a re-initialize superseded us. Don't touch self._agent;
            # reclaim the subprocesses this load spawned so they don't leak.
            if gen != self._init_gen or self._builder is not builder:
                try:
                    builder.mcp_shutdown()
                except Exception:
                    pass
                return
            if not had_tools:
                return  # nothing to add — the fast-path agent is already correct
            # Atomic reference swap (GIL-protected). Rebuild is cheap: MCP is
            # cached, so build() re-registers tools + re-renders the prompt
            # without spawning. Do the swap BEFORE setting ready so a waiting
            # turn sees the MCP-equipped agent.
            self._agent = builder.build(session_log=self._session_log)
        finally:
            ready.set()

    def _shutdown_mcp(self):
        if self._builder is not None:
            try:
                self._builder.mcp_shutdown()
            except Exception:
                pass

    def _shutdown_lsp(self):
        if self._builder is not None:
            try:
                self._builder.lsp_shutdown()
            except Exception:
                pass

    def _shutdown_egress(self):
        if self._builder is not None:
            try:
                self._builder.shutdown_egress()
            except Exception:
                pass

    def _load_or_create_log(self, session_id: str, entry: dict[str, Any]) -> tuple[SessionLog, bool]:
        """Load the session's JSONL log, or create a fresh one (D6: no <id>.json).

        Returns ``(log, created)`` — ``created`` is True only when a brand-new
        log was minted (SessionStart uses it for the resume/startup payload).
        """
        if session_id:
            try:
                header, events = self._log_store.load(session_id)
                return SessionLog.from_events(header, events), False
            except SessionNotFoundError:
                pass
        header = SessionHeader(
            id=session_id or "session",
            createdAt=int(time.time() * 1000),
            cwd=self._cwd or None,
            provider=entry.get("provider", ""),
            model=entry.get("model_name", ""),
            apiType=entry.get("api_type", ""),
        )
        log = SessionLog.create(header)
        self._log_store.create(header)
        return log, True

    def _bind_persister(self) -> None:
        """(Re)bind incremental JSONL persistence to the active session log.

        Called whenever ``self._session_log`` is loaded/created/reloaded (init,
        undo truncate), because the log object is replaced at those points and an
        observer bound to the old object would stop firing.
        """
        if self._persister is not None:
            self._persister.dispose()
            self._persister = None
        if self._session_log is not None and self._session_id:
            self._persister = IncrementalPersister(
                self._log_store, self._session_id, self._session_log
            )

    def _handle_chat_send(self, req_id: Any, params: dict[str, Any]):
        if self._agent is None:
            _write_dict({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": "Agent not initialized"}})
            return

        self._cancel_chat()

        # Per-message model + reasoning-effort override. The composer always sends
        # the session's current selection; a change here switches the provider
        # (model switch rebuilds the agent) or just mutates the reasoning effort.
        self._apply_model_override(params)

        message = params["message"]
        history = params.get("history", [])
        # Python owns history now (D6): when the session log is loaded, derive the
        # working history from it and ignore the desktop's stale <id>.json copy.
        # Capture the log object ONCE: undo (truncate) replaces self._session_log
        # on the dispatch thread while the turn thread is still starting up, and
        # deriving history from one object while binding the agent to another
        # trips run()'s history-vs-surface guard (ValueError).
        log = self._session_log
        if log is not None:
            history = log.derive_messages()
        # First turn of the session (empty prior history) — used to trigger a
        # one-time LLM-generated session title once the reply completes.
        is_first_turn = not history
        if isinstance(message, list):
            user_text = "\n".join(b["text"] for b in message if b.get("type") == "text")
        elif isinstance(message, str):
            user_text = message
        else:
            user_text = str(message)

        # Pre-turn checkpoint: snapshot the workspace before the agent acts so
        # the user can rewind to the exact state that preceded this message. The
        # sha + the pre-turn history length are the undo anchor for THIS message;
        # emit them as a turn_start event so the desktop attaches a per-message
        # undo button. When checkpoints are unavailable (no git), pre_sha is None
        # and we skip the event — undo degrades to no button.
        if self._checkpoints is not None:
            try:
                pre_sha = self._checkpoints.snapshot(self._session_id, user_text[:80])
                if pre_sha:
                    _write_dict({
                        "jsonrpc": "2.0", "method": "chat/stream",
                        "params": {
                            "type": "turn_start", "agent_id": "root",
                            "checkpoint_id": pre_sha,
                            "log_seq": log.seq if log is not None else len(history),
                        },
                    })
            except Exception:
                traceback.print_exc(file=sys.stderr)

        cbs = JsonRpcCallbacks(self._policy)
        self._callbacks = cbs
        # Attach the callbacks as this turn's subagent tracker so TaskTool/
        # SkillTool emit agent lifecycle, tool, and streamed-text events (tagged
        # with each child's agent_id). Child builders inherit it via
        # _child_builder. Reset next turn when a fresh cbs is built.
        if self._builder is not None:
            self._builder.set_tracker(cbs)

        def _run():
            # Wait (bounded) for the background MCP loader so the FIRST message
            # after initialize still gets MCP tools. The wait lives here — inside
            # the turn's own thread — so the main dispatch loop stays responsive
            # (tool approvals, cancel) while we block. Once loaded, the event is
            # already set and this returns instantly; if the loader hangs past
            # the cap we proceed without MCP rather than stalling the turn.
            #
            # Poll in short slices instead of one long wait so a Stop pressed
            # mid-wait is honored promptly: cbs.cancel() flips _cancelled, and we
            # bail out of the wait rather than making the user sit through the
            # full cap before cancellation takes effect at the loop's top.
            if not self._mcp_ready.is_set():
                deadline = time.monotonic() + _MCP_READY_WAIT_S
                while not self._mcp_ready.wait(timeout=0.1):
                    if cbs._cancelled or time.monotonic() >= deadline:
                        break
            # Pin THIS turn's agent up front, AFTER the wait so we capture the
            # MCP-equipped agent the loader swapped in. A later background swap
            # (shouldn't happen post-ready) keeps reset(), run() and aclose() on
            # one consistent instance for this turn.
            agent = self._agent
            # Attach the log captured at send time (the agent may have been
            # hot-swapped by the MCP loader after initialize) and compute this
            # turn's memory/skill injections (empty when unchanged). Using the
            # captured `log` — not self._session_log — keeps it consistent with
            # the `history` derived above even when undo swapped self._session_log
            # while this thread was starting up.
            if log is not None:
                agent.session_log = log
            # Keep the builder's live-log reference fresh so build_child records
            # spawn events against the log THIS turn is running on.
            if self._builder is not None and log is not None:
                self._builder.attach_session_log(log)
            injections = (
                self._builder.injections_for_turn() if self._builder else []
            )
            # One-shot SessionStart feedback: prepended to the FIRST turn only
            # (cleared here so later turns don't re-inject it).
            if self._session_start_feedback:
                injections = [
                    ("hook", fb) for fb in self._session_start_feedback
                ] + injections
                self._session_start_feedback = []
            # ProactorEventLoop needs a ThreadPoolExecutor for run_in_executor.
            # Without one, the shared pool may not be configured in daemon threads.
            loop = asyncio.new_event_loop()
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
            loop.set_default_executor(executor)
            # Silence the benign post-close 'Event loop is closed' RuntimeError
            # that ProactorEventLoop's TLS teardown can raise from a late
            # call_soon (see _quiet_closed_loop_handler). Must be set on THIS
            # loop — an orphaned Task reports via its own loop's handler.
            loop.set_exception_handler(_quiet_closed_loop_handler)
            asyncio.set_event_loop(loop)
            # Rebuild the provider's async client so it binds to THIS turn's loop.
            # The client (httpx under AsyncOpenAI) binds its
            # connection pool to the loop of the first request; since each turn
            # runs in a fresh loop that we close below, a client carried over from
            # turn 1 would be bound to a dead loop and turn 2 would hang until it
            # timed out. reset() must happen before agent.run.
            #
            # Keep the returned handle so the teardown below closes exactly THIS
            # turn's client. Stop now interrupts the stream via task cancellation,
            # but a cancelled turn's teardown can still race a rapidly-started
            # next turn (every turn shares the ONE provider on self._agent), so
            # closing the provider's *current* client in the old turn's finally
            # would instead close whichever client the new turn just reset to,
            # killing the new turn's stream with an httpx.ReadError. Closing the
            # handle returned here is defense-in-depth against that overlap.
            turn_client = agent.provider.reset()
            # Schedule the turn as a Task and register it so _cancel_chat() can
            # interrupt an in-flight stream from the dispatch thread. (Stop used
            # to only flip a flag the loop checks at approval boundaries, so a
            # stream kept running and overlapped the next turn — racing on the
            # shared agent/provider/session-log.) Register BEFORE the cancelled
            # check so a Stop landing during setup still aborts the turn instead
            # of letting it stream to completion.
            main_task = loop.create_task(
                agent.run(
                    user_text, history=history, callbacks=cbs,
                    injections=injections,
                )
            )
            self._turn_task = main_task
            self._turn_loop = loop
            if cbs._cancelled:
                main_task.cancel()
            try:
                result = loop.run_until_complete(main_task)
                # Notification hooks: fire-and-forget at turn end. Runs on its
                # own daemon thread so a slow hook never stalls this thread's
                # teardown (the checkpoint/title work below still runs).
                self._fire_notification("Turn completed")
                # A compaction this turn may have folded earlier memory/skill/mode
                # injections into the summary — force a fresh injection next turn.
                if agent.compacted_this_turn and self._builder is not None:
                    self._builder.invalidate_injections()
                # Post-turn checkpoint: snapshot after the agent finished. The
                # post-turn commit's parent is the pre-turn snapshot, so its
                # summary is exactly what THIS turn changed — stream it as a
                # turn_diff event so the UI shows a "changed files" card.
                if self._checkpoints is not None:
                    try:
                        post_sha = self._checkpoints.snapshot(self._session_id, "after turn")
                        if post_sha:
                            files = self._checkpoints.summary(post_sha)
                            if files:
                                _write_dict({
                                    "jsonrpc": "2.0", "method": "chat/stream",
                                    "params": {
                                        "type": "turn_diff", "agent_id": "root",
                                        "checkpoint_id": post_sha, "files": files,
                                    },
                                })
                    except Exception:
                        traceback.print_exc(file=sys.stderr)
                # First-turn session title: one short LLM call to replace the
                # desktop's first-line default with something specific. Runs on
                # this turn's still-open loop (before aclose in finally) so the
                # provider client is live. Best-effort and time-boxed — any
                # failure just leaves the first-line title in place. Skipped
                # when the turn ended in the network fallback: the API is
                # unreachable, so the title call would just burn its timeout.
                if (
                    is_first_turn
                    and result.text
                    and result.text != NETWORK_FALLBACK_TEXT
                ):
                    try:
                        title = loop.run_until_complete(
                            asyncio.wait_for(
                                _generate_title(
                                    agent.provider, user_text, result.text
                                ),
                                timeout=20.0,
                            )
                        )
                        if title:
                            _write_dict({
                                "jsonrpc": "2.0", "method": "chat/stream",
                                "params": {"type": "title_suggested", "title": title},
                            })
                    except Exception:
                        traceback.print_exc(file=sys.stderr)
                # Return the updated history so the desktop persists it — both
                # multi-turn context and undo's truncation anchor depend on the
                # saved history file staying in sync.
                _write_dict({"jsonrpc": "2.0", "id": req_id, "result": {
                    "stop_reason": "end_turn",
                    "text": result.text,
                    "history": result.history,
                    "usage": result.cache_usage,
                    "timing": {
                        "ttft_ms": result.ttft_ms,
                        "gen_ms": result.gen_ms,
                        "out_tokens": result.out_tokens,
                    },
                }})
            except asyncio.CancelledError:
                # Turn interrupted mid-stream by Stop / a superseding send. The
                # agent loop already labeled the turn "aborted" and closed it on
                # the log, so there is no history to persist — returning None
                # tells the desktop not to overwrite the session (same contract
                # as the approval-gate cancel below).
                _write_dict({"jsonrpc": "2.0", "id": req_id, "result": {
                    "stop_reason": "cancelled", "text": None, "history": None,
                }})
            except asyncio.TimeoutError:
                # Should not fire under normal conditions — there is no outer
                # wait_for around agent.run(). If this does fire, preserve the
                # behaviour: don't let a partial turn overwrite the session file.
                _write_dict({"jsonrpc": "2.0", "id": req_id, "result": {
                    "stop_reason": "timeout",
                    "text": "[Agent loop timed out]",
                    "history": None,
                }})
            except _CancelledError:
                _write_dict({"jsonrpc": "2.0", "id": req_id, "result": {
                    "stop_reason": "cancelled", "text": None, "history": None,
                }})
            except Exception as e:
                traceback.print_exc(file=sys.stderr)
                _write_dict({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": str(e)}})
            finally:
                # Clear the turn registration — but only if it still points at
                # THIS turn. A superseding send may already have registered its
                # own task before this (cancelled) turn's thread unwinds, and we
                # must not clobber that newer registration.
                if self._turn_task is main_task:
                    self._turn_task = None
                    self._turn_loop = None
                # Catch-up flush. Events are persisted incrementally as the turn
                # runs (IncrementalPersister), so a process kill mid-turn already
                # leaves every committed event on disk and load() repairs the open
                # turn on restart. This is a no-op unless an earlier write failed.
                if self._persister is not None:
                    self._persister.flush()
                # Close the client's pool on this live loop before closing the
                # loop — otherwise the GC finalizes it later on a dead loop and
                # raises "Event loop is closed".
                try:
                    loop.run_until_complete(agent.provider.aclose(turn_client))
                except Exception:
                    pass
                # aclose() schedules the TLS transport's connection-lost
                # callback via call_soon; a mid-stream cancel can also leave an
                # httpx streaming response's async generator unclosed. Drain
                # both while the loop is still alive so their teardown does NOT
                # land on the closed loop as an orphaned "Event loop is closed"
                # Task. shutdown_asyncgens aclose()s abandoned generators; the
                # sleep(0) yields one iteration so queued call_soon callbacks
                # run. Best-effort — the exception handler above is the net.
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                    loop.run_until_complete(asyncio.sleep(0))
                except Exception:
                    pass
                loop.close()
                executor.shutdown(wait=False)

        threading.Thread(target=_run, daemon=True).start()

    def _set_mode(self, mode: str):
        """Switch development mode mid-session. Because 'plan' changes the
        registered toolset (hard read-only isolation), the agent is rebuilt — the
        builder caches MCP, so this does NOT restart MCP subprocesses. Non-plan
        switches only change approval behavior but rebuild too for uniformity."""
        try:
            self._policy.set_mode(mode)
        except ValueError:
            return
        if self._builder is not None:
            self._builder.with_mode(mode)
            self._agent = self._builder.build(session_log=self._session_log)

    def _grants_snapshot(self) -> dict[str, Any]:
        paths = self._grants.snapshot() if self._grants else []
        return {"paths": paths}

    def _forbid_read_snapshot(self) -> dict[str, Any]:
        if self._read_denies is None:
            return {"paths": [], "protect_sensitive": False}
        return {
            "paths": self._read_denies.snapshot(),
            "protect_sensitive": self._read_denies.protect_sensitive(),
        }

    def _set_forbid_read(self, params: dict[str, Any]) -> dict[str, Any]:
        """Replace the read-denylist and/or flip the built-in sensitive-file
        toggle, then rebuild the agent so the new set is picked up by the read
        fence + shell sandbox on the next turn. Unlike grants there is NO
        enforcement-side reconcile — a read deny leaves no on-disk label to
        restore."""
        if self._read_denies is None:
            from cluxmate.core.read_denies import ReadDenyStore
            self._read_denies = ReadDenyStore()
        if "paths" in params:
            wanted = []
            for p in params.get("paths", []):
                if isinstance(p, str) and p.strip():
                    wanted.append(self._read_denies.add(p))
            for d in self._read_denies.snapshot():
                if d not in wanted:
                    self._read_denies.remove(d)
        if "protect_sensitive" in params:
            self._read_denies.set_protect_sensitive(
                bool(params["protect_sensitive"])
            )
        if self._builder is not None:
            self._builder.with_read_denies(self._read_denies)
            self._agent = self._builder.build(session_log=self._session_log)
        return self._forbid_read_snapshot()

    def _ssrf_snapshot(self) -> dict[str, Any]:
        cfg = getattr(self, "_ssrf_config", None)
        return cfg.snapshot() if cfg is not None else {"allow": [], "block_extra": []}

    def _set_ssrf_config(self, params: dict[str, Any]) -> dict[str, Any]:
        """Replace the SSRF allow/block lists. Invalid entries raise ValueError
        (→ error response). The config is mtime-cached per request, so NO agent
        rebuild is needed — unlike grants, a network config change takes effect
        on the next web_fetch without killing the session."""
        if getattr(self, "_ssrf_config", None) is None:
            from cluxmate.core.ssrf_config import SsrConfig
            self._ssrf_config = SsrConfig()
        return self._ssrf_config.set_rules(
            [e for e in params.get("allow", []) if isinstance(e, str)],
            [e for e in params.get("block_extra", []) if isinstance(e, str)],
        )

    def _egress_snapshot(self) -> dict[str, Any]:
        cfg = getattr(self, "_egress_config", None)
        return cfg.snapshot() if cfg is not None else {"mode": "shared"}

    def _set_egress_config(self, params: dict[str, Any]) -> dict[str, Any]:
        """Replace the egress mode and rebuild the agent (the mode is baked
        into the sandbox backend at build time). Invalid mode raises ValueError
        (→ error response)."""
        if getattr(self, "_egress_config", None) is None:
            from cluxmate.core.egress_config import EgressConfig
            self._egress_config = EgressConfig()
        result = self._egress_config.set_mode(params.get("mode", "shared"))
        if self._builder is not None:
            self._builder.with_egress(self._egress_config)
            self._agent = self._builder.build(session_log=self._session_log)
        return result

    def _set_grants(self, paths: list[str]) -> dict[str, Any]:
        """Replace the grant set. Revoked folders are restored Low → Medium
        (reconcile); the agent is rebuilt so the new set is picked up by the
        fence and the shell sandbox on the next turn."""
        if self._grants is None:
            from cluxmate.core.grants import GrantStore
            self._grants = GrantStore()
        wanted = []
        for p in paths:
            if isinstance(p, str) and p.strip():
                wanted.append(self._grants.add(p))
        removed = [g for g in self._grants.snapshot() if g not in wanted]
        restored: list[str] = []
        for g in removed:
            if self._grants.remove(g) is not None:
                # Enforcement-side reconcile: drop the Low label back to Medium
                # so the folder is no longer writable by low-IL children.
                from cluxmate.tools._sandbox import WindowsLowILSandbox
                if WindowsLowILSandbox.restore_path(g):
                    restored.append(g)
        # Rebuild so a fresh fence + shell sandbox reflect the new grant set.
        if self._builder is not None:
            self._builder.with_grants(self._grants)
            self._agent = self._builder.build(session_log=self._session_log)
        return {"paths": self._grants.snapshot(), "restored": restored}

    def _handle_truncate(self, session_id: str, seq: int):
        """Undo: truncate the JSONL log to ``seq`` and reload the in-memory log."""
        if not session_id:
            return
        # Subagent logs whose spawn events fall at/after `seq` become unreachable
        # once the parent log is truncated — delete them so an undo fully reverts
        # (history + subagent traces) instead of leaking orphaned <child>.jsonl.
        for child_id in orphaned_subagent_ids(self._log_store, session_id, seq):
            try:
                self._log_store.delete(child_id)
            except Exception:
                traceback.print_exc(file=sys.stderr)
        self._log_store.truncate(session_id, seq)
        if self._session_log is not None and self._session_log.id == session_id:
            try:
                header, events = self._log_store.load(session_id)
                self._session_log = SessionLog.from_events(header, events)
                self._bind_persister()
                if self._agent is not None:
                    self._agent.session_log = self._session_log
                if self._builder is not None:
                    self._builder.attach_session_log(self._session_log)
            except SessionNotFoundError:
                pass

    def _cancel_chat(self):
        if self._callbacks:
            self._callbacks.cancel()
        self._callbacks = None
        # Interrupt the in-flight turn itself, not just its approval gates.
        # call_soon_threadsafe is the cross-thread way to cancel a task running
        # on the turn's own loop. Without this, a Stop during streaming leaves
        # the turn running to completion on a loop that overlaps the next send,
        # and the two turns race on the shared agent/provider/session-log
        # (ValueError history mismatch, httpx.ReadError, …).
        task = self._turn_task
        loop = self._turn_loop
        if task is not None and loop is not None and not task.done():
            loop.call_soon_threadsafe(task.cancel)

    def _tool_decision(self, call_id: str, approved: bool, always: bool = False, selected: list[int] | None = None):
        if self._callbacks:
            self._callbacks.resolve_tool(call_id, approved, always, selected)

    def _question_decision(self, call_id: str, answers: list[dict[str, Any]]):
        if self._callbacks:
            self._callbacks.resolve_question(call_id, answers)

    def _build_provider(self, model_id: str):
        """Build a provider from a config model entry id.

        Re-reads the entry (with env-resolved api_key) from config by id, so the
        desktop never has to ship the api_key over the RPC — keys live only in
        ~/.cluxmate/config.json. Falls back to the active model when the id is
        empty or missing. Returns (provider, model_name, context_1m) so the
        caller can set the builder's display model and compaction budget.
        """
        from cluxmate.core.config import ConfigManager
        from cluxmate.core.providers.factory import build_provider
        config = ConfigManager()
        entry = config.get_model(model_id) if model_id else None
        if entry is None:
            entry = config.get_active_model()
        if entry is None:
            raise ValueError("No model configured")
        return (
            build_provider(entry),
            entry.get("model_name", ""),
            bool(entry.get("context_1m", False)),
            entry,
        )

    def _apply_model_override(self, params: dict[str, Any]):
        """Apply a chat/send model_id / reasoning_effort override.

        A changed ``model_id`` rebuilds the provider + agent (the builder caches
        MCP, so this does not re-spawn subprocesses) and resets the reasoning
        effort to the new entry's default — unless the request names an explicit
        effort. An effort-only change just mutates the shared provider in place.
        """
        model_id = params.get("model_id")
        effort = params.get("reasoning_effort") or None

        if model_id and model_id != self._model_id:
            provider, model_name, context_1m, entry = self._build_provider(model_id)
            if effort is None:
                from cluxmate.core.reasoning import default_for
                effort = default_for(entry)
            if effort:
                provider.set_reasoning_effort(effort)
            self._model_id = entry.get("id", "")
            self._reasoning_effort = effort
            if self._builder is not None:
                self._builder.with_provider(provider)
                self._builder.with_model(model_name)
                self._builder.with_context_1m(context_1m)
                self._agent = self._builder.build(session_log=self._session_log)
            return

        if effort != self._reasoning_effort and self._agent is not None:
            self._agent.provider.set_reasoning_effort(effort)
            self._reasoning_effort = effort


def main():
    JsonRpcServer().run()


if __name__ == "__main__":
    main()
