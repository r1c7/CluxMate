"""ReadFile tool — read files with optional offset/limit."""

from pathlib import Path
from typing import Any

from .base import BaseTool


class ReadFileTool(BaseTool):
    """Read the contents of a file."""

    def __init__(self, workdir: str | None = None):
        self._workdir = workdir

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "Read a file from the local filesystem. "
            "Supports offset and limit for reading specific line ranges."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read.",
                },
                "offset": {
                    "type": "integer",
                    "description": "Line number to start reading from (1-indexed).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of lines to read.",
                },
            },
            "required": ["path"],
        }

    async def execute(
        self,
        path: str,
        offset: int | None = None,
        limit: int | None = None,
    ) -> str:
        file_path = Path(path)
        if not file_path.is_absolute():
            file_path = (Path(self._workdir) if self._workdir else Path.cwd()) / file_path

        if not file_path.exists():
            return f"Error: file not found: {path}"

        if file_path.is_dir():
            return f"Error: path is a directory: {path}"

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"Error reading file: {e}"

        lines = content.split("\n")
        total = len(lines)

        start = 0
        end = total
        if offset is not None:
            if offset < 1:
                return f"Error: offset must be >= 1, got {offset}"
            start = min(offset - 1, total)
        if limit is not None:
            end = min(start + limit, total)

        selected = lines[start:end]
        numbered = []
        for i, line in enumerate(selected, start=start + 1):
            prefix = f"{i}\t"
            numbered.append(f"{prefix}{line}")

        return "\n".join(numbered)
