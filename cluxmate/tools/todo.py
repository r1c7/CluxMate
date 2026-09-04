"""Todo tool — the model-declared task tracking list.

Whole-list replacement: every call
REPLACES the previous list (there are no partial updates, no per-item edits),
and the agent loop appends the canonical list to the session log as a log-only
``todo/write`` event (whole-value state event, see ``BaseTool.session_event``).
UIs and replay folds read the current list from those events; the model-visible
transcript is unaffected.

The list is a *declaration*, not a verified fact: "completed" means the model
marked it completed — the host validates only shape, never whether the work
actually happened (the completion audit reconciles claims vs tool evidence
separately).
"""

from typing import Any

from cluxmate.core.session_log import TODO_WRITE_EVENT
from .base import BaseTool

# The valid todo statuses, as a runtime tuple for input validation.
STATUSES = ("pending", "in_progress", "completed")

_DESCRIPTION = (
    "Record and update a structured task list for the current work. Send the "
    "ENTIRE list every call — it REPLACES the previous list (there are no "
    "partial updates, no per-item edits). Use it to plan multi-step work and "
    "show progress: add one todo per concrete step before you start. Mark "
    "every todo being actively worked on `in_progress` — several at once when "
    "work genuinely runs in parallel (concurrent tool calls or subagents), one "
    "for sequential work; while work remains, at least one task should be "
    "`in_progress`. Mark a todo `completed` the moment it is done (do not batch "
    "completions), and allow no `in_progress` item only once all work is "
    "complete. Skip the list for trivial single-step tasks. Statuses: "
    "`pending` (not started), `in_progress` (being worked on now), `completed` "
    "(finished)."
)


def canonical_todos(raw: Any) -> list[dict[str, str]]:
    """Validate and normalize a model-supplied todo list.

    Trimmed non-empty unique content and known statuses. Raises
    :class:`ValueError` on invalid input — ``execute`` lets
    ``run_safe`` turn that into an error result, and the agent loop then
    appends no ``todo/write`` event (a failed call must not overwrite the last
    good list).
    """
    if not isinstance(raw, list):
        raise ValueError("invalid todos: expected an array of {content, status}")
    todos: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("invalid todos: each item must be an object")
        content = item.get("content")
        status = item.get("status")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("invalid todos: `content` must be a non-empty string")
        if status not in STATUSES:
            raise ValueError(f"invalid todos: `status` must be one of {list(STATUSES)}")
        content = content.strip()
        if content in seen:
            raise ValueError(f"invalid todos: duplicate content {content!r}")
        seen.add(content)
        todos.append({"content": content, "status": status})
    return todos


class TodoTool(BaseTool):
    """Whole-list task tracking; the canonical list rides the session log."""

    #: Log-only event the agent loop appends after an executed, non-error call.
    session_event: str | None = TODO_WRITE_EVENT

    @property
    def name(self) -> str:
        return "todo_write"

    @property
    def description(self) -> str:
        return _DESCRIPTION

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "The COMPLETE task list, replacing any previous list.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "What the task is — a short imperative line.",
                            },
                            "status": {
                                "type": "string",
                                "enum": list(STATUSES),
                                "description": (
                                    "pending (not started) | in_progress (being "
                                    "worked on now) | completed (done)."
                                ),
                            },
                        },
                        "required": ["content", "status"],
                    },
                },
            },
            "required": ["todos"],
        }

    @property
    def risk_level(self) -> str:
        # Writes only a session-log event — no filesystem, no network.
        return "safe"

    async def execute(self, todos: Any = None, **kwargs: Any) -> str:
        canonical = canonical_todos(todos)
        counts = {
            status: sum(1 for t in canonical if t["status"] == status)
            for status in STATUSES
        }
        return (
            f"Updated todo list: {counts['pending']} pending, "
            f"{counts['in_progress']} in progress, "
            f"{counts['completed']} completed."
        )

    def result_data(self, args: dict[str, Any], output: str) -> dict[str, Any] | None:
        """The canonical whole list — the ``todo/write`` event payload."""
        return {"todos": canonical_todos(args.get("todos"))}
