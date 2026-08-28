"""ListDir tool — list directory contents."""

from pathlib import Path
from typing import Any

from .base import BaseTool
from ._fence import ReadDenied, ReadFence


class ListDirTool(BaseTool):
    """List files and directories in a given path."""

    def __init__(self, workdir: str | None = None, fence: ReadFence | None = None):
        self._workdir = workdir
        self._fence = fence or ReadFence()

    @property
    def name(self) -> str:
        return "list_dir"

    @property
    def description(self) -> str:
        return (
            "List the contents of a directory. "
            "Returns file and directory names with type indicators."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path to list.",
                },
            },
            "required": ["path"],
        }

    async def execute(self, path: str = "", directory: str = "") -> str:
        dir_path_str = path or directory
        if not dir_path_str:
            return "Error: path is required"
        dir_path = Path(dir_path_str)
        if not dir_path.is_absolute():
            dir_path = (Path(self._workdir) if self._workdir else Path.cwd()) / dir_path

        try:
            dir_path = self._fence.check(dir_path)
        except ReadDenied as e:
            return f"Error: {e}"

        if not dir_path.exists():
            return f"Error: path not found: {dir_path_str}"

        if not dir_path.is_dir():
            return f"Error: not a directory: {dir_path_str}"

        try:
            entries = sorted(dir_path.iterdir(), key=lambda p: (p.is_file(), p.name))
        except PermissionError:
            return f"Error: permission denied: {dir_path_str}"

        if not entries:
            return "Directory is empty."

        lines = []
        for entry in entries:
            if self._fence.is_denied(entry):
                continue
            prefix = "  " if entry.is_file() else "[dir] "
            lines.append(f"{prefix} {entry.name}")

        return "\n".join(lines) if lines else "Directory is empty."
