"""DeleteFile tool — remove a file from the filesystem."""

from pathlib import Path
from typing import Any

from .base import BaseTool
from ._fence import WriteFence
from ._sandbox import ESCALATION_SCHEMA_FIELDS


class DeleteFileTool(BaseTool):
    """Delete a single file.

    Native os-level removal rather than shelling out to `rm`/`del`. On Windows
    `del` prompts ("Are you sure? / delete read-only file?") which, under a
    non-interactive subprocess, hangs until the tool timeout. A direct unlink
    has no such trap. Deleting whole directory trees is intentionally NOT
    supported here — that stays an explicit bash decision the user approves.
    """

    def __init__(self, workdir: str | None = None, fence: WriteFence | None = None):
        self._workdir = workdir
        self._fence = fence or WriteFence(workdir)

    @property
    def name(self) -> str:
        return "delete_file"

    @property
    def description(self) -> str:
        return (
            "Delete a single file from the filesystem. Does not delete "
            "directories — use bash for that if truly needed. "
            "Deletion is a last resort: only use this when the user explicitly "
            "asks to remove a file, or to clean up a temporary file you created "
            "earlier in this same session. Never delete a user's own files just "
            "because they seem unused, leftover, or to tidy up — leave them, or "
            "use mv to move/rename instead. When unsure, ask first."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to delete.",
                },
                **ESCALATION_SCHEMA_FIELDS,
            },
            "required": ["path"],
        }

    @property
    def risk_level(self) -> str:
        return "dangerous"

    async def execute(
        self,
        path: str,
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

        if file_path.is_dir():
            return (
                f"Error: path is a directory: {path}. "
                f"delete_file only removes single files."
            )

        try:
            file_path.unlink()
        except Exception as e:
            return f"Error deleting file: {e}"

        return f"Deleted {file_path}"
