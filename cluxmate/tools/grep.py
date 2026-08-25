"""Grep tool — search for text patterns in files."""

import re
from pathlib import Path
from typing import Any

from .base import BaseTool


class GrepTool(BaseTool):
    """Search for a pattern in files."""

    def __init__(self, workdir: str | None = None):
        self._workdir = workdir

    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return (
            "Search for a regex pattern in files under a directory. "
            "Returns matching file paths and line content. "
            "Skips binary files and common directories like .git, __pycache__, node_modules."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory or file to search in.",
                },
                "pattern": {
                    "type": "string",
                    "description": "Regular expression pattern to search for.",
                },
            },
            "required": ["path", "pattern"],
        }

    async def execute(self, path: str, pattern: str) -> str:
        search_path = Path(path)
        if not search_path.is_absolute():
            search_path = (Path(self._workdir) if self._workdir else Path.cwd()) / search_path

        if not search_path.exists():
            return f"Error: path not found: {path}"

        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"Error: invalid regex pattern: {e}"

        skip_dirs = {
            ".git", "__pycache__", "node_modules", ".venv", "venv",
            ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
        }

        results = []
        if search_path.is_file():
            self._search_file(search_path, regex, results)
        else:
            for file_path in search_path.rglob("*"):
                if file_path.is_file():
                    parts = set(file_path.parts)
                    if parts & skip_dirs:
                        continue
                    self._search_file(file_path, regex, results)

        if not results:
            return "No matches found."

        output = "\n".join(results[:200])
        if len(results) > 200:
            output += f"\n\n[Output truncated: {len(results)} matches found, showing first 200]"
        return output

    def _search_file(
        self, file_path: Path, regex: re.Pattern, results: list[str]
    ) -> None:
        if len(results) >= 200:
            return
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return

        for i, line in enumerate(content.split("\n"), 1):
            if regex.search(line):
                results.append(f"{file_path}:{i}: {line.strip()[:200]}")
