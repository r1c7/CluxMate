"""AskUserQuestion tool — pause for a human answer.

The tool itself does NOT collect the answer: the agent loop asks the frontend's
``ask_question`` callback and injects the answer as ``_answer`` (mirroring how
``multi_edit`` receives the user's edit selection as ``_selected``). The tool is
a thin, frontend-agnostic wrapper that validates the questions and echoes the
answer back as a plain JSON tool result, which the model consumes directly.
"""

import json
from typing import Any, TYPE_CHECKING

from .base import BaseTool

if TYPE_CHECKING:
    from cluxmate.core.builder import AgentBuilder


class AskUserQuestionTool(BaseTool):
    """Ask the user a concise question and return their answer as the tool result."""

    def __init__(self, builder: "AgentBuilder"):
        self._builder = builder

    @property
    def name(self) -> str:
        return "ask_user_question"

    @property
    def description(self) -> str:
        return (
            "Ask the user a concise question when you need confirmation, a choice, "
            "or missing information before proceeding. Always use this tool to ask "
            "the user — never ask in plain reply text. Send one or more questions, "
            "each with a stable id that will be echoed in the answer."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "description": "Questions to ask the user before continuing.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "Stable id for this question; echoed in the answer.",
                            },
                            "question": {
                                "type": "string",
                                "description": "The specific question to ask the user.",
                            },
                            "header": {
                                "type": "string",
                                "description": (
                                    'Optional short heading for the question, such as '
                                    '"Confirm" or "Choose Mode".'
                                ),
                            },
                            "options": {
                                "type": "array",
                                "description": (
                                    "Optional choices to show the user. If you recommend "
                                    'one, put it first and append "(Recommended)" to that label.'
                                ),
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {
                                            "type": "string",
                                            "description": "Short user-facing option label.",
                                        },
                                        "description": {
                                            "type": "string",
                                            "description": "One sentence explaining the tradeoff or impact.",
                                        },
                                    },
                                    "required": ["label"],
                                },
                            },
                            "multi_select": {
                                "type": "boolean",
                                "description": "Whether the user may select more than one option. Defaults to false.",
                            },
                        },
                        "required": ["id", "question"],
                    },
                },
            },
            "required": ["questions"],
        }

    @property
    def risk_level(self) -> str:
        # Read-only interaction — auto-approves, never raises a permission prompt.
        return "safe"

    async def execute(
        self,
        questions: list[dict[str, Any]] | None = None,
        _answer: dict[str, Any] | None = None,
    ) -> str:
        if not questions:
            return "Error: ask_user_question requires at least one question."
        if _answer is None:
            return (
                "Error: ask_user_question is not supported in this interface — "
                "no question UI is available to collect the user's answer. "
                "Include the unresolved question or decision in your response "
                "instead of assuming a default."
            )
        return json.dumps(_answer, ensure_ascii=False)
