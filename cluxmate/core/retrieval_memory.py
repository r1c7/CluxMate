"""Retrieval memory — lexical recall over episodic facts + AGENTS.md chunks.

Episodic facts are one-markdown-file-per-fact under ``~/.cluxmate/memory/``
(global) and ``<cwd>/.cluxmate/memory/`` (project). AGENTS.md is optionally
chunked into the same in-memory FTS5 (trigram) index. Recall runs per real
human turn and returns a low-authority ``<memory-recall>`` block or None.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cluxmate.core.memory import MemoryManager

_CONFIG_DEFAULTS = {
    "enabled": False,
    "max_facts": 4,
    "max_chars": 2400,
    "include_agents_md": False,
}

_GENERIC_QUERIES = frozenset({
    "continue", "go on", "ok", "okay", "yes", "no",
    "继续", "继续吧", "好的", "对", "是", "嗯",
})

_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_WORD_RE = re.compile(r"[a-zA-Z0-9_]+")


class RetrievalConfig:
    """mtime/size-cached snapshot of ``~/.cluxmate/retrieval-memory.json``."""

    def __init__(self, path: str | Path | None = None):
        if path is None:
            path = Path.home() / ".cluxmate" / "retrieval-memory.json"
        self._path = Path(path)
        self._cache: tuple[tuple[int, int], dict[str, Any]] | None = None

    @property
    def path(self) -> Path:
        return self._path

    def snapshot(self) -> dict[str, Any]:
        try:
            st = self._path.stat()
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            return dict(_CONFIG_DEFAULTS)
        if self._cache is not None and self._cache[0] == key:
            return self._cache[1]
        data = self._load()
        self._cache = (key, data)
        return data

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._path.read_text("utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return dict(_CONFIG_DEFAULTS)
        if not isinstance(raw, dict):
            return dict(_CONFIG_DEFAULTS)
        out = dict(_CONFIG_DEFAULTS)
        if isinstance(raw.get("enabled"), bool):
            out["enabled"] = raw["enabled"]
        if isinstance(raw.get("max_facts"), int) and raw["max_facts"] > 0:
            out["max_facts"] = raw["max_facts"]
        if isinstance(raw.get("max_chars"), int) and raw["max_chars"] > 0:
            out["max_chars"] = raw["max_chars"]
        if isinstance(raw.get("include_agents_md"), bool):
            out["include_agents_md"] = raw["include_agents_md"]
        return out
