"""MemoryManager — project & global memory injected as a synthetic user message.

Memory is plain markdown the user maintains to give the agent durable context
(project conventions, commands, gotchas). Two files, both named ``AGENTS.md``:

- global:  ``~/.cluxmate/AGENTS.md``   (applies to every project)
- project: ``<cwd>/AGENTS.md``          (this project only)

``AGENTS.md`` is authoritative — it matches the OpenAI Codex convention, so
projects already carrying a Codex ``AGENTS.md`` work unchanged. When an
``AGENTS.md`` is missing, ``render()`` falls back to the equivalent Claude Code
memory file (``CLAUDE.md``) so projects
already carrying Claude Code conventions still work: project ``<cwd>/CLAUDE.md``
and global ``~/.claude/CLAUDE.md``. Writes (``append``) always target
``AGENTS.md`` and never ``CLAUDE.md``.

``render()`` concatenates them global-first, project-second, so project memory
appears later and can reinforce/override the global guidance. The rendered text
is injected each turn as a ``source:"memory"`` user message (see
``AgentBuilder.render_injections`` / ``injections_for_turn``), so edits — and a
freshly ``/init``-generated file — take effect on the very next message.
"""

from __future__ import annotations

from pathlib import Path

from cluxmate.tools._fileio import read_normalized, write_preserving

# Cap each file so a huge AGENTS.md can't blow up the context window. Measured
# in CHARACTERS: the unit the token estimate (chars/4) and the model actually pay
# for. A byte cap would be a different number for the same content (CJK is ~3
# bytes/char) and the two call sites below would disagree about it.
MAX_MEMORY_CHARS = 64 * 1024

MEMORY_FILENAME = "AGENTS.md"
LEGACY_FILENAME = "CLAUDE.md"


class MemoryManager:
    """Read and merge global + project memory for a working directory."""

    def __init__(self, cwd: str):
        self._cwd = str(Path(cwd).resolve()) if cwd else str(Path.cwd())

    def global_path(self) -> Path:
        return Path.home() / ".cluxmate" / MEMORY_FILENAME

    def project_path(self) -> Path:
        return Path(self._cwd) / MEMORY_FILENAME

    def _legacy_path(self, scope: str) -> Path:
        if scope == "global":
            return Path.home() / ".claude" / LEGACY_FILENAME
        return Path(self._cwd) / LEGACY_FILENAME

    def _read_source(self, scope: str) -> Path:
        """Actual file to read for a scope: AGENTS.md if present, else CLAUDE.md.

        Returns the AGENTS.md path even when neither exists so _read still
        resolves cleanly (and reads an empty string). Writes always use
        path_for(), never this fallback.
        """
        primary = self.path_for(scope)
        if primary.is_file():
            return primary
        legacy = self._legacy_path(scope)
        return legacy if legacy.is_file() else primary

    def _read_text(self, path: Path) -> str:
        """Raw file text (UTF-8; unreadable -> "")."""
        try:
            return path.read_text("utf-8", errors="replace")
        except OSError:
            return ""

    def _read(self, path: Path) -> str:
        text = self._read_text(path)
        if len(text) > MAX_MEMORY_CHARS:
            text = text[:MAX_MEMORY_CHARS] + "\n\n[memory truncated]"
        return text.strip()

    def render(self) -> str:
        """Merged memory text (global first, then project), or "" if none."""
        sections: list[str] = []
        g = self._read(self._read_source("global"))
        if g:
            sections.append(f"<global_memory>\n{g}\n</global_memory>")
        p = self._read(self._read_source("project"))
        if p:
            sections.append(f"<project_memory>\n{p}\n</project_memory>")
        return "\n\n".join(sections)

    def path_for(self, scope: str = "project") -> Path:
        """Resolve the AGENTS.md path for a scope ('global' or 'project')."""
        return self.global_path() if scope == "global" else self.project_path()

    def append(self, content: str, scope: str = "project") -> Path:
        """Append a memory entry to the scoped AGENTS.md; returns its path.

        Creates the file (and ~/.cluxmate/ for global scope) if missing.
        Preserves the file's existing newline style via the shared _fileio
        helpers so we don't flip line endings on Windows. A blank separator is
        inserted before the new entry when the file already has content — the
        caller supplies its own markdown structure.
        """
        content = content.strip()
        path = self.path_for(scope)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            existing, newline = read_normalized(path)
        else:
            existing, newline = "", "\n"
        merged = f"{existing.rstrip()}\n\n{content}\n" if existing.strip() else f"{content}\n"
        write_preserving(path, merged, newline)
        return path

    def is_over_limit(self, scope: str = "project") -> bool:
        """True when the scoped memory is being truncated (append still allowed).

        Measures exactly what _read clamps: characters of the file actually read
        (AGENTS.md, else the CLAUDE.md fallback). Sharing both the unit and the
        source resolution is the point -- a stat() size check on path_for()
        reported a different answer for CJK files and for the legacy fallback.
        """
        return len(self._read_text(self._read_source(scope))) > MAX_MEMORY_CHARS
