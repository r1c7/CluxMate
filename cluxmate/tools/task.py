"""TaskTool — spawn subagents for independent work."""

import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, TYPE_CHECKING

from cluxmate.core.session_log_store import IncrementalPersister
from cluxmate.core.subagent_scheduler import normalize_claim, workspace_claim

from .base import BaseTool

if TYPE_CHECKING:
    from cluxmate.core.builder import AgentBuilder


class TaskTool(BaseTool):
    """Delegate work to a subagent and return its result."""

    # End-reason kinds of a child's last turn that mean the child did NOT
    # finish normally. The parent must not present such a result as a clean
    # completion (run-settlement whitelist pattern).
    _ABNORMAL_END_KINDS = frozenset(
        {"aborted", "interrupted", "max-turns", "max-tokens", "error"}
    )

    @staticmethod
    def _child_end_kind(child: Any) -> str | None:
        """``kind`` of the child's most recent ``turn/end`` event, if any."""
        log = getattr(child, "session_log", None)
        if log is None:
            return None
        for event in reversed(log.events):
            if event.type == "turn/end":
                return (event.data.get("reason") or {}).get("kind")
        return None

    def __init__(self, builder: "AgentBuilder"):
        self._builder = builder

    @property
    def name(self) -> str:
        return "task"

    @property
    def description(self) -> str:
        return (
            "Launch a subagent to handle a specific sub-task independently. "
            "Use this for clearly independent work that would benefit from "
            "a separate context.\n\n"
            "Available subagent types (each on its own line with description, "
            "tool set and model):\n"
            f"{self._type_catalog()}\n\n"
            "Parameters:\n"
            "- subagent_type: one of the types listed above\n"
            "- description: A short summary of the task\n"
            "- prompt: The detailed task instructions for the subagent\n"
            "- write_paths: Optional. Workspace files/directories this subagent "
            "will write; omitted it claims the whole workspace and serializes "
            "against every other writing subagent"
        )

    def _type_catalog(self) -> str:
        """One line per spawnable type (spec §2.7), in deterministic catalog
        order: ``- <slug>: <description> (tools: …; model: inherit|id)``."""
        allowed = (
            self._builder.allowed_subagent_slugs()
            or ["general-purpose", "explore"]
        )
        lines = []
        for slug in allowed:
            atype = self._builder.agent_type(slug)
            if atype is None:
                continue
            lines.append(
                f"- {slug}: {atype.description} "
                f"(tools: {', '.join(atype.tools)}; model: {atype.model})"
            )
        return "\n".join(lines) if lines else ", ".join(allowed)

    @property
    def input_schema(self) -> dict[str, Any]:
        # Advertise only the subagent types this agent may actually spawn
        # (mirrors the runtime allowlist in execute, and the web_fetch
        # plan-mode precedent of schema-level restriction). A builder with no
        # configured types never registers this tool, so the fallback is only
        # a safety net.
        allowed = (
            self._builder.allowed_subagent_slugs()
            or ["general-purpose", "explore"]
        )
        return {
            "type": "object",
            "properties": {
                "subagent_type": {
                    "type": "string",
                    "description": f"Type of subagent:\n{self._type_catalog()}",
                    "enum": allowed,
                },
                "description": {
                    "type": "string",
                    "description": "Short summary of what the subagent should do.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Detailed task instructions for the subagent.",
                },
                "write_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional. Files or directories (relative to the workspace) this "
                        "subagent will write. If you omit it, the subagent claims the WHOLE "
                        "workspace and is serialized against every other writing subagent — "
                        "declare disjoint paths when you want two writers to run in parallel."
                    ),
                },
            },
            "required": ["subagent_type", "description", "prompt"],
        }

    @property
    def risk_level(self) -> str:
        return "write"

    @property
    def spill_cwd(self) -> str | None:
        """Spill a long child report to the parent's workspace.

        A subagent's report can exceed the inline cap; head/tail keeps its
        trailing ``**Status**`` line and the full text stays readable.
        """
        return getattr(self._builder, "cwd", None)

    def _nested(self) -> bool:
        """A spawn issued BY a subagent (depth ≥ 1) fails fast when full."""
        return getattr(self._builder, "depth", 0) > 0

    def _claim_for(self, profile, write_paths):
        """(claim, dropped) for this spawn — read-only types never claim."""
        if profile is None or profile.readonly:
            return frozenset(), []
        cwd = getattr(self._builder, "cwd", None) or os.getcwd()
        # Type guard: the model can hand over anything despite the array schema.
        # A non-list counts as "not declared" instead of list("src") exploding
        # into single-character paths.
        if not isinstance(write_paths, list):
            write_paths = None
        if write_paths:
            claim, dropped = normalize_claim(cwd, list(write_paths))
            if not claim:
                # EVERY declared entry landed outside the workspace. An empty
                # claim would read as read-only (no serialization at all), which
                # silently buys MORE parallelism than declaring nothing — fall
                # back to the conservative whole-workspace claim instead.
                return workspace_claim(cwd), dropped
            return claim, dropped
        return workspace_claim(cwd), []

    async def execute(
        self,
        subagent_type: str = "general-purpose",
        description: str = "",
        prompt: str = "",
        write_paths: list[str] | None = None,
    ) -> str:
        # Schema is an array, but the model can pass anything; a non-list is
        # treated as "not declared" (a bare string must not become char paths).
        if not isinstance(write_paths, list):
            write_paths = None
        # Allowlist gate: an agent may only spawn the subagent types it was
        # configured with. In particular, `explore` children only carry
        # ["explore"] (see _child_builder), so they cannot request a
        # general-purpose grandchild and smuggle write access past the
        # read-only gate. Returns an error string (not a raise) so the
        # denied result feeds back into the agent loop and it picks another
        # path.
        allowed = self._builder.allowed_subagent_slugs()
        if subagent_type not in allowed:
            return (
                f"Subagent type '{subagent_type}' is not allowed from this "
                f"agent (allowed: {allowed}). Use one of the permitted types "
                f"or complete the work yourself."
            )
        # Admission control (core/subagent_scheduler.py): take a slot BEFORE
        # building the child so the child builder inherits this loop's
        # scheduler. Read-only types never claim; a writing type that declares
        # nothing claims the whole workspace (parallel writers serialize).
        profile = self._builder.agent_type(subagent_type)
        claim, dropped = self._claim_for(profile, write_paths)
        scheduler = self._builder._scheduler_for_loop()
        wait_start = time.monotonic()
        ticket: Any = None
        ticket = await scheduler.acquire(claim, nested=self._nested(), slug=subagent_type)
        queued_ms = int((time.monotonic() - wait_start) * 1000)
        if ticket is None:
            return (
                "Subagent concurrency is full and this spawn is nested, so it was "
                "refused instead of queued (a queued child would hold its parent's "
                "slot and deadlock). Finish the current sub-task yourself, or "
                "reduce parallelism."
            )
        child_id = uuid.uuid4().hex
        tracker = getattr(self._builder, "_tracker", None)
        parent_id = getattr(self._builder, "_agent_id", "root")
        depth = getattr(self._builder, "_depth", 0) + 1
        child_persister = None
        try:
            # Everything from here on runs inside the try so the finally below
            # frees the scheduler slot on EVERY path — success, tool error,
            # cancel, or an on_agent_start/build_child failure.
            if tracker is not None:
                await tracker.on_agent_start(
                    child_id, parent_id, subagent_type, description, depth, prompt
                )

            child = self._builder.build_child(
                subagent_type, description, child_id,
                write_paths=list(write_paths or []), queued_ms=queued_ms,
            )
            # Scope callbacks to this child so its tool/text events are tagged with
            # child_id. Subagents run autonomously (auto-approve) — their tool calls
            # stream for the tree but never prompt the user.
            child_cbs = (
                tracker.scoped(child_id, auto_approve=True)
                if tracker is not None
                else None
            )
            # Persist the subagent's own event log incrementally (its header was
            # already written by build_child), so a crash mid-subagent leaves the
            # partial trace on disk instead of losing it until the finally below.
            store = getattr(self._builder, "_log_store", None)
            if child.session_log is not None and store is not None:
                child_persister = IncrementalPersister(
                    store, child_id, child.session_log
                )

            started = time.monotonic()
            result = await child.run(prompt, history=[], callbacks=child_cbs)
            text = result.text or "(subagent returned no output)"
            # Completion honesty gate: the child's own event log is the source
            # of truth for how its turn actually ended — its reply text alone
            # may claim success. The end kind drives the machine-readable
            # header below, so the parent can't miss a truncated child.
            end_kind = self._child_end_kind(child)
            abnormal = end_kind in self._ABNORMAL_END_KINDS
            status = _normalize_status(_reported_status(text), end_kind)
            text, blocked = await self._run_subagent_stop_hook(
                subagent_type, description, prompt, child_id, text, error=None,
            )
            if blocked:
                status = "blocked"
            header = _result_header(
                subagent_type,
                status,
                end_kind=end_kind if abnormal else None,
                turns=result.turns,
                max_turns=getattr(child, "max_turns", None),
                out_tokens=result.out_tokens,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                queued_ms=queued_ms,
            )
            parts = [header]
            if dropped:
                # Tell the PARENT (which declared the paths) which entries were
                # ignored — the child prompt never mentions claims (spec §3.2:
                # "dropped ... and surfaced in the tool result").
                parts.append(
                    "[note: write_paths entries outside the workspace were "
                    f"ignored: {', '.join(dropped)}]"
                )
            parts += ["", text]
            text = "\n".join(parts)
            if tracker is not None:
                # Mirror the reload path's classification
                # (session_log_store.py: "done" for completed/max-tokens/
                # max-turns, else "error") so the live agent-tree event agrees
                # with what the desktop reconstructs from the child JSONL.
                await tracker.on_agent_end(
                    child_id,
                    (
                        "done"
                        if end_kind in (None, "completed", "max-tokens", "max-turns")
                        else "error"
                    ),
                    text,
                    input_tokens=(result.cache_usage or {}).get("input_tokens", 0),
                    output_tokens=result.out_tokens,
                )
            return text
        except Exception as e:
            msg = f"Subagent failed: {e}"
            msg, _ = await self._run_subagent_stop_hook(
                subagent_type, description, prompt, child_id, msg, error=str(e),
            )
            # spec §4.1: the error text rides INSIDE the header brackets.
            msg = (
                _result_header(subagent_type, "failed", note=str(e)) + "\n\n" + msg
            )
            if tracker is not None:
                await tracker.on_agent_end(child_id, "error", msg)
            return msg
        finally:
            # Free the scheduler slot on every path — success, tool error,
            # cancel — so a cancelled task call never leaks a claim.
            if ticket is not None:
                scheduler.release(ticket)
            # Catch-up flush on every path — success, tool error, cancel — then
            # detach the observer so the (now-finished) child log stops flushing.
            if child_persister is not None:
                child_persister.flush()
                child_persister.dispose()

    async def _run_subagent_stop_hook(
        self,
        subagent_type: str,
        description: str,
        prompt: str,
        child_id: str,
        text: str,
        *,
        error: str | None,
    ) -> tuple[str, bool]:
        """Run SubagentStop hooks after a subagent settles and adjust the reply.

        Returns ``(text, blocked)``. The subagent has already finished, so
        "block" cannot stop it — instead the block reason REPLACES the
        subagent's reply in the parent's tool result (the model is told the
        result was rejected). Feedback is appended to the reply as extra
        context. Not run on cancellation: a cancelled subagent (turn cancelled)
        propagates CancelledError, which is not caught by ``except Exception``
        above.
        """
        hooks_manager = getattr(self._builder, "_hooks_manager", None)
        hooks = hooks_manager() if hooks_manager is not None else None
        if hooks is None or not hooks.has_event("SubagentStop"):
            return text, False
        hr = await hooks.run_event(
            "SubagentStop",
            extra={
                "subagent_id": child_id,
                "subagent_type": subagent_type,
                "task_description": description,
                "prompt": prompt,
                "response": text,
                "error": error,
            },
        )
        if hr.blocked:
            return hr.reason or "[SubagentStop hook blocked the subagent result]", True
        for fb in hr.feedback:
            text = f"{text}\n\n[SubagentStop hook context]\n{fb}"
        return text, False


# The child prompt requires a `**Status**: ...` line; we parse it back instead
# of trusting prose. Bounded scan: a status line buried 20 lines down is not a
# status line.
_STATUS_LINE_RE = re.compile(
    r"^\s*\*\*Status\*\*\s*:\s*(success|partial|failed|blocked)\b", re.IGNORECASE
)
_STATUS_SCAN_LINES = 20


def _reported_status(text: str) -> str | None:
    """The child's own **Status** claim, if it made one."""
    for line in (text or "").splitlines()[:_STATUS_SCAN_LINES]:
        m = _STATUS_LINE_RE.match(line)
        if m:
            return m.group(1).lower()
    return None


def _normalize_status(reported: str | None, end_kind: str | None) -> str:
    """Reconcile the child's claim with how its turn actually ended.

    The child's own ``turn/end`` is authoritative: a child that claims success
    but was cut off at max-turns is reported as partial. A child that already
    admits a worse outcome keeps its word (more specific than ours).
    """
    if reported in ("partial", "failed", "blocked"):
        return reported
    if end_kind in TaskTool._ABNORMAL_END_KINDS:
        return "partial"
    return reported or "unknown"


def _fmt_tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _result_header(
    slug: str,
    status: str,
    *,
    end_kind: str | None = None,
    turns: int | None = None,
    max_turns: int | None = None,
    out_tokens: int = 0,
    elapsed_ms: int = 0,
    queued_ms: int = 0,
    note: str | None = None,
) -> str:
    """One machine-readable line prepended to every subagent report."""
    parts = [f"subagent: {slug}", f"status={status}"]
    if end_kind:
        parts.append(f"end={end_kind}")
    if turns is not None:
        parts.append(f"turns={turns}/{max_turns}" if max_turns else f"turns={turns}")
    if out_tokens:
        parts.append(f"out={_fmt_tokens(out_tokens)} tok")
    if elapsed_ms > 0:
        parts.append(f"{elapsed_ms / 1000:.0f}s")
    if queued_ms > 50:
        parts.append(f"wait={queued_ms / 1000:.1f}s")
    if note:
        parts.append(note)
    return "[" + " | ".join(parts) + "]"
