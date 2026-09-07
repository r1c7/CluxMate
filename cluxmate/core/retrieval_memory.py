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


@dataclass(frozen=True)
class Doc:
    source: str       # "global" | "project"
    kind: str         # "fact" | "agents_md"
    doc_id: str       # fact id (kind="fact") or path (kind="agents_md")
    body: str
    path: str
    fingerprint: str


def _chunk_text(text: str, max_chars: int = 1600) -> list[str]:
    """Split markdown on ``## `` headings, hard-splitting over-long sections."""
    text = text.strip()
    if not text:
        return []
    parts: list[str] = []
    current = ""
    for line in text.splitlines():
        if line.startswith("## ") and current.strip():
            parts.append(current.strip())
            current = line + "\n"
        else:
            current += line + "\n"
    if current.strip():
        parts.append(current.strip())
    chunks: list[str] = []
    for part in parts:
        if len(part) <= max_chars:
            chunks.append(part)
            continue
        for para in part.split("\n\n"):
            para = para.strip()
            if not para:
                continue
            chunks.append(para if len(para) <= max_chars else para[:max_chars])
    return chunks


class RetrievalMemory:
    """Fact store + in-memory FTS5 (trigram) index with per-turn recall."""

    def __init__(self, cwd: str, config: RetrievalConfig):
        self._cwd = str(Path(cwd).resolve()) if cwd else str(Path.cwd())
        self._config = config
        self._conn = sqlite3.connect(":memory:")
        self._conn.execute(
            "CREATE VIRTUAL TABLE fts USING fts5(doc_id UNINDEXED, body, tokenize='trigram')"
        )
        self._docs: list[Doc] = []
        self._fingerprints: tuple[str, ...] = ()

    def _facts_dir(self, scope: str) -> Path:
        if scope == "global":
            return Path.home() / ".cluxmate" / "memory" / "global" / "facts"
        return Path(self._cwd) / ".cluxmate" / "memory" / "facts"

    def enabled(self) -> bool:
        return self._config.snapshot()["enabled"]

    def remember(self, content: str, scope: str = "project") -> str:
        content = (content or "").strip()
        if not content:
            return "Error: content is empty — nothing to record."
        if scope not in ("global", "project"):
            scope = "project"
        d = self._facts_dir(scope)
        d.mkdir(parents=True, exist_ok=True)
        fact_id = uuid.uuid4().hex[:12]
        (d / f"{fact_id}.md").write_text(content + "\n", encoding="utf-8")
        return f"Recorded fact {fact_id} ({scope} memory)."

    def forget(self, fact_id: str) -> str:
        fact_id = (fact_id or "").strip()
        if not fact_id or any(c in fact_id for c in "/\\") or fact_id in (".", ".."):
            return "Error: invalid fact id."
        for scope in ("global", "project"):
            path = self._facts_dir(scope) / f"{fact_id}.md"
            if path.is_file():
                path.unlink()
                return f"Forgot fact {fact_id}."
        return f"Error: no fact with id {fact_id}."
