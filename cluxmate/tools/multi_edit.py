"""MultiEdit tool — apply multiple search-replace edits across files."""

from pathlib import Path
from typing import Any

from .base import BaseTool
from ._fileio import read_normalized, write_preserving
from ._fence import WriteFence
from ._sandbox import ESCALATION_SCHEMA_FIELDS


class MultiEditTool(BaseTool):
    """Apply multiple search-replace edits across files in one batch."""

    def __init__(self, workdir: str | None = None, fence: WriteFence | None = None):
        self._workdir = workdir
        self._fence = fence or WriteFence(workdir)

    @property
    def name(self) -> str:
        return "multi_edit"

    @property
    def description(self) -> str:
        return (
            "Apply multiple search-replace edits across files in one batch. "
            "Each edit specifies a path, old_string (exact match), and new_string. "
            "Use this when you need to modify several files at once — the user "
            "will review all diffs before any changes are applied. "
            "Prefer single-file search_replace for one-off edits."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "edits": {
                    "type": "array",
                    "description": (
                        "List of edits to apply. Each edit is an object with "
                        "path, old_string, and new_string. Maximum 20 edits "
                        "for reviewability."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Path to the file to modify.",
                            },
                            "old_string": {
                                "type": "string",
                                "description": "The exact string to find and replace.",
                            },
                            "new_string": {
                                "type": "string",
                                "description": "The replacement string.",
                            },
                        },
                        "required": ["path", "old_string", "new_string"],
                    },
                },
                **ESCALATION_SCHEMA_FIELDS,
            },
            "required": ["edits"],
        }

    @property
    def risk_level(self) -> str:
        return "write"

    async def execute(
        self,
        edits: list[dict[str, str]],
        _selected: list[int] | None = None,
        sandbox_permissions: str | None = None,
        justification: str | None = None,
    ) -> str:
        if not edits:
            return "Error: edits must be a non-empty array."

        # Filter by user-selected indices when provided. _selected is injected
        # by AgentLoop post-approval — the Agent cannot pass it.
        indices = _selected if _selected is not None else list(range(len(edits)))
        escalate = sandbox_permissions == "danger-full-access"

        results: list[tuple[int, str, str]] = []  # (index, status, message)
        for i in indices:
            if i < 0 or i >= len(edits):
                results.append((i, "✗", f"index {i} out of range"))
                continue

            edit = edits[i]
            path_str = edit.get("path", "")
            old_string = edit.get("old_string", "")
            new_string = edit.get("new_string", "")

            file_path = Path(path_str)
            if not file_path.is_absolute():
                file_path = (
                    Path(self._workdir) if self._workdir else Path.cwd()
                ) / file_path

            violation = self._fence.check_message(file_path, escalate=escalate)
            if violation:
                results.append((i, "✗", violation))
                continue
            file_path = file_path.resolve(strict=False)

            if not file_path.exists():
                results.append((i, "✗", f"file not found: {path_str}"))
                continue

            try:
                content, newline = read_normalized(file_path)
            except Exception as e:
                results.append((i, "✗", f"error reading {path_str}: {e}"))
                continue

            old_norm = old_string.replace("\r\n", "\n")
            new_norm = new_string.replace("\r\n", "\n")

            occurrences = content.count(old_norm)
            if occurrences == 0:
                results.append((
                    i, "✗",
                    f"old_string not found in {path_str} — check exact whitespace and indentation",
                ))
                continue

            new_content = content.replace(old_norm, new_norm, 1)
            try:
                write_preserving(file_path, new_content, newline)
            except Exception as e:
                results.append((i, "✗", f"error writing {path_str}: {e}"))
                continue

            results.append((i, "✓", path_str))

        applied = sum(1 for _, status, _ in results if status == "✓")
        total = len(results)
        lines = [f"Applied {applied}/{total} edits:"]
        for _, status, msg in results:
            lines.append(f"  {status} {msg}")
        return "\n".join(lines)
