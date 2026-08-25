"""MultiWrite tool — create or overwrite multiple files in one batch."""

from pathlib import Path
from typing import Any

from .base import BaseTool
from ._fileio import detect_newline, write_preserving
from ._fence import WriteFence
from ._sandbox import ESCALATION_SCHEMA_FIELDS


class MultiWriteTool(BaseTool):
    """Create or overwrite multiple files in one batch.

    The batch counterpart to write_file: the user reviews every file's content
    as a diff before any is written, then approves the whole set at once. Use
    when creating several new files together (e.g. scaffolding a module) instead
    of firing many separate write_file calls.
    """

    def __init__(self, workdir: str | None = None, fence: WriteFence | None = None):
        self._workdir = workdir
        self._fence = fence or WriteFence(workdir)

    @property
    def name(self) -> str:
        return "multi_write"

    @property
    def description(self) -> str:
        return (
            "Create or overwrite multiple files in one batch. Each entry has a "
            "path and the full content to write. Use this when creating several "
            "files at once — the user reviews all diffs before anything is "
            "written. Parent directories are created automatically. Prefer "
            "single-file write_file for one file, and search_replace/multi_edit "
            "to modify parts of existing files. Maximum 20 files for reviewability."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "description": (
                        "List of files to write. Each file is an object with a "
                        "path and content. Maximum 20 files."
                    ),
                    "items": {
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
                        },
                        "required": ["path", "content"],
                    },
                },
                **ESCALATION_SCHEMA_FIELDS,
            },
            "required": ["files"],
        }

    @property
    def risk_level(self) -> str:
        return "write"

    async def execute(
        self,
        files: list[dict[str, str]],
        _selected: list[int] | None = None,
        sandbox_permissions: str | None = None,
        justification: str | None = None,
    ) -> str:
        if not files:
            return "Error: files must be a non-empty array."

        # Filter by user-selected indices when provided. _selected is injected
        # by AgentLoop post-approval — the Agent cannot pass it (not in schema).
        indices = _selected if _selected is not None else list(range(len(files)))
        escalate = sandbox_permissions == "danger-full-access"

        results: list[tuple[int, str, str]] = []  # (index, status, message)
        for i in indices:
            if i < 0 or i >= len(files):
                results.append((i, "✗", f"index {i} out of range"))
                continue

            entry = files[i]
            path_str = entry.get("path", "")
            content = entry.get("content", "")

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

            if file_path.is_dir():
                results.append((i, "✗", f"path is a directory: {path_str}"))
                continue

            # Preserve an existing file's newline style; new files default to LF
            # so freshly written code stays consistent across platforms.
            existed = file_path.exists()
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
                results.append((i, "✗", f"error writing {path_str}: {e}"))
                continue

            verb = "overwrote" if existed else "created"
            results.append((i, "✓", f"{verb} {path_str}"))

        applied = sum(1 for _, status, _ in results if status == "✓")
        total = len(results)
        lines = [f"Wrote {applied}/{total} files:"]
        for _, status, msg in results:
            lines.append(f"  {status} {msg}")
        return "\n".join(lines)
