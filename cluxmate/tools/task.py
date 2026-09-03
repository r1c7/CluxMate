"""TaskTool — spawn subagents for independent work."""

import uuid
from typing import Any, TYPE_CHECKING

from cluxmate.core.session_log_store import IncrementalPersister

from .base import BaseTool

if TYPE_CHECKING:
    from cluxmate.core.builder import AgentBuilder


class TaskTool(BaseTool):
    """Delegate work to a subagent and return its result."""

    # End-reason kinds of a child's last turn that mean the child did NOT
    # finish normally. The parent must not present such a result as a clean
    # completion (run-settlement whitelist pattern, deepseek-harness
    # packages/subagent/subagent/src/run-settlement.ts).
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
            "Parameters:\n"
            "- subagent_type: 'general-purpose' (read+write+execute) or "
            "'explore' (read-only research)\n"
            "- description: A short summary of the task\n"
            "- prompt: The detailed task instructions for the subagent"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        # Advertise only the subagent types this agent may actually spawn
        # (mirrors the runtime allowlist in execute, and the web_fetch
        # plan-mode precedent of schema-level restriction). A builder with no
        # configured types never registers this tool, so the fallback is only
        # a safety net.
        allowed = (
            getattr(self._builder, "_subagent_types", None)
            or ["general-purpose", "explore"]
        )
        return {
            "type": "object",
            "properties": {
                "subagent_type": {
                    "type": "string",
                    "description": (
                        f"Type of subagent: {', '.join(allowed)}."
                    ),
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
            },
            "required": ["subagent_type", "description", "prompt"],
        }

    @property
    def risk_level(self) -> str:
        return "write"

    async def execute(
        self,
        subagent_type: str = "general-purpose",
        description: str = "",
        prompt: str = "",
    ) -> str:
        # Allowlist gate: an agent may only spawn the subagent types it was
        # configured with. In particular, `explore` children only carry
        # ["explore"] (see _child_builder), so they cannot request a
        # general-purpose grandchild and smuggle write access past the
        # read-only gate. Returns an error string (not a raise) so the
        # denied result feeds back into the agent loop and it picks another
        # path.
        allowed = getattr(self._builder, "_subagent_types", None) or []
        if subagent_type not in allowed:
            return (
                f"Subagent type '{subagent_type}' is not allowed from this "
                f"agent (allowed: {allowed}). Use one of the permitted types "
                f"or complete the work yourself."
            )
        child_id = uuid.uuid4().hex
        tracker = getattr(self._builder, "_tracker", None)
        parent_id = getattr(self._builder, "_agent_id", "root")
        depth = getattr(self._builder, "_depth", 0) + 1

        if tracker is not None:
            await tracker.on_agent_start(
                child_id, parent_id, subagent_type, description, depth, prompt
            )

        child = self._builder.build_child(subagent_type, description, child_id)
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
        child_persister = None
        if child.session_log is not None and store is not None:
            child_persister = IncrementalPersister(store, child_id, child.session_log)

        try:
            result = await child.run(prompt, history=[], callbacks=child_cbs)
            text = result.text or "(subagent returned no output)"
            # Completion honesty gate: the child's own event log is the source
            # of truth for how its turn actually ended — its reply text alone
            # may claim success. A non-completed end reason (aborted, max
            # turns/tokens, error) is surfaced explicitly so the parent never
            # treats a truncated/aborted child result as a clean completion.
            end_kind = self._child_end_kind(child)
            if end_kind in self._ABNORMAL_END_KINDS:
                text = (
                    f"[Subagent did not complete normally ({end_kind}). "
                    f"Treat its output as partial and do not rely on it as a "
                    f"finished result.]\n\n{text}"
                )
            text = await self._run_subagent_stop_hook(
                subagent_type, description, prompt, child_id, text, error=None,
            )
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
            msg = await self._run_subagent_stop_hook(
                subagent_type, description, prompt, child_id, msg, error=str(e),
            )
            if tracker is not None:
                await tracker.on_agent_end(child_id, "error", msg)
            return msg
        finally:
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
    ) -> str:
        """Run SubagentStop hooks after a subagent settles and adjust the reply.

        The subagent has already finished, so "block" cannot stop it — instead
        the block reason REPLACES the subagent's reply in the parent's tool
        result (the model is told the result was rejected). Feedback is appended
        to the reply as extra context. Not run on cancellation: a cancelled
        subagent (turn cancelled) propagates CancelledError, which is not caught
        by ``except Exception`` above.
        """
        hooks_manager = getattr(self._builder, "_hooks_manager", None)
        hooks = hooks_manager() if hooks_manager is not None else None
        if hooks is None or not hooks.has_event("SubagentStop"):
            return text
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
            return hr.reason or "[SubagentStop hook blocked the subagent result]"
        for fb in hr.feedback:
            text = f"{text}\n\n[SubagentStop hook context]\n{fb}"
        return text
