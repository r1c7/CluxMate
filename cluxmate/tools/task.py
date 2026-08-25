"""TaskTool — spawn subagents for independent work."""

import uuid
from typing import Any, TYPE_CHECKING

from cluxmate.core.session_log_store import IncrementalPersister

from .base import BaseTool

if TYPE_CHECKING:
    from cluxmate.core.builder import AgentBuilder


class TaskTool(BaseTool):
    """Delegate work to a subagent and return its result."""

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
            if tracker is not None:
                await tracker.on_agent_end(
                    child_id,
                    "done",
                    text,
                    input_tokens=(result.cache_usage or {}).get("input_tokens", 0),
                    output_tokens=result.out_tokens,
                )
            return text
        except Exception as e:
            msg = f"Subagent failed: {e}"
            if tracker is not None:
                await tracker.on_agent_end(child_id, "error", msg)
            return msg
        finally:
            # Catch-up flush on every path — success, tool error, cancel — then
            # detach the observer so the (now-finished) child log stops flushing.
            if child_persister is not None:
                child_persister.flush()
                child_persister.dispose()
