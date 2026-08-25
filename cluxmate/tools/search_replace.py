"""SearchReplace tool — find and replace text in files."""

from pathlib import Path
from typing import Any

from .base import BaseTool
from ._fileio import read_normalized, write_preserving
from ._fence import WriteFence
from ._sandbox import ESCALATION_SCHEMA_FIELDS


class SearchReplaceTool(BaseTool):
    """Search for a string in a file and replace it."""

    def __init__(self, workdir: str | None = None, fence: WriteFence | None = None):
        self._workdir = workdir
        self._fence = fence or WriteFence(workdir)

    @property
    def name(self) -> str:
        return "search_replace"

    @property
    def description(self) -> str:
        return (
            "Search for a string in a file and replace it. "
            "The old_string must match exactly, including whitespace. "
            "Use replace_all=True to replace all occurrences."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
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
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences (default: false, replace first only).",
                    "default": False,
                },
                **ESCALATION_SCHEMA_FIELDS,
            },
            "required": ["path", "old_string", "new_string"],
        }

    @property
    def risk_level(self) -> str:
        return "write"

    async def execute(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        sandbox_permissions: str | None = None,
        justification: str | None = None,
    ) -> str:
        file_path = Path(path)
        if not file_path.is_absolute():
            file_path = (Path(self._workdir) if self._workdir else Path.cwd()) / file_path

        try:
            file_path = self._fence.check(
                file_path,
                escalate=sandbox_permissions == "danger-full-access",
            )
        except Exception as e:
            return f"Error: {e}"

        if not file_path.exists():
            return f"Error: file not found: {path}"

        try:
            # Read LF-normalized so matching is newline-agnostic; remember the
            # file's original style so the write doesn't flip line endings
            # (a single-line edit must not rewrite every ending to CRLF).
            content, newline = read_normalized(file_path)
        except Exception as e:
            return f"Error reading file: {e}"

        # Normalize the model-supplied strings too, so a CRLF file still matches
        # against the LF-normalized content.
        old_norm = old_string.replace("\r\n", "\n")
        new_norm = new_string.replace("\r\n", "\n")

        occurrences = content.count(old_norm)
        if occurrences == 0:
            return (
                f"Error: old_string not found in {file_path}. "
                f"The text must match exactly, including indentation and "
                f"whitespace. Check for tabs vs spaces, trailing spaces, and "
                f"that the snippet exists verbatim."
            )

        if not replace_all and occurrences > 1:
            return (
                f"Error: old_string is not unique in {file_path} — it appears "
                f"{occurrences} times. Provide more surrounding context to make "
                f"the match unique, or pass replace_all=true to replace every "
                f"occurrence."
            )

        if replace_all:
            count = occurrences
            new_content = content.replace(old_norm, new_norm)
        else:
            count = 1
            new_content = content.replace(old_norm, new_norm, 1)

        try:
            write_preserving(file_path, new_content, newline)
        except Exception as e:
            return f"Error writing file: {e}"

        return f"Replaced {count} occurrence(s) in {file_path}"
