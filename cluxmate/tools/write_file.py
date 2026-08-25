"""WriteFile tool — create or overwrite a file with given content."""

from pathlib import Path
from typing import Any

from .base import BaseTool
from ._fileio import detect_newline, write_preserving
from ._fence import WriteFence
from ._sandbox import ESCALATION_SCHEMA_FIELDS


class WriteFileTool(BaseTool):
    """Create a new file or overwrite an existing one.

    Native filesystem write (Path.write_text) rather than shelling out to
    bash. On Windows, `echo >`/redirection through cmd.exe mangles quoting,
    encoding, and special characters — a direct write avoids all of that and
    works identically across platforms.
    """

    def __init__(self, workdir: str | None = None, fence: WriteFence | None = None):
        self._workdir = workdir
        self._fence = fence or WriteFence(workdir)

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return (
            "Create a new file or overwrite an existing file with the given "
            "content. Parent directories are created automatically. To edit "
            "part of an existing file, prefer search_replace."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to write.",
                },
                "content": {
                    "type": "string",
                    "description": "The full content to write to the file.",
                },
                **ESCALATION_SCHEMA_FIELDS,
            },
            "required": ["path", "content"],
        }

    @property
    def risk_level(self) -> str:
        return "write"

    async def execute(
        self,
        path: str,
        content: str = "",
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

        if file_path.is_dir():
            return f"Error: path is a directory: {path}"

        existed = file_path.exists()
        # Preserve an existing file's newline style; new files default to LF
        # (not the platform's CRLF) so freshly written code stays consistent
        # across platforms rather than getting Windows line endings.
        newline = "\n"
        if existed:
            try:
                raw = file_path.read_bytes().decode("utf-8", errors="replace")
                newline = detect_newline(raw)
            except Exception:
                newline = "\n"
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            write_preserving(file_path, content, newline)
        except Exception as e:
            return f"Error writing file: {e}"

        verb = "Overwrote" if existed else "Created"
        n_lines = content.count("\n") + 1 if content else 0
        return f"{verb} {file_path} ({n_lines} line(s), {len(content)} chars)"
