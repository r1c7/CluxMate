"""Tests for retrieval-memory config, fact store, chunking, and recall."""

import json
import threading
from pathlib import Path

import pytest

from cluxmate.core.retrieval_memory import (
    RetrievalConfig,
    RetrievalMemory,
    _chunk_text,
    _tokenize,
)


def _home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    return home


def test_config_defaults_off(tmp_path):
    cfg = RetrievalConfig(tmp_path / "retrieval-memory.json")
    assert cfg.snapshot() == {
        "enabled": False,
        "max_facts": 4,
        "max_chars": 2400,
        "include_agents_md": False,
    }


def test_config_reads_values(tmp_path):
    p = tmp_path / "retrieval-memory.json"
    p.write_text(json.dumps({"enabled": True, "max_facts": 2, "max_chars": 500}), encoding="utf-8")
    assert RetrievalConfig(p).snapshot()["enabled"] is True
    assert RetrievalConfig(p).snapshot()["max_facts"] == 2
    assert RetrievalConfig(p).snapshot()["max_chars"] == 500


def test_config_corrupt_file_yields_defaults(tmp_path):
    p = tmp_path / "retrieval-memory.json"
    p.write_text("{not json", encoding="utf-8")
    assert RetrievalConfig(p).snapshot()["enabled"] is False


def test_config_ignores_bad_types(tmp_path):
    p = tmp_path / "retrieval-memory.json"
    p.write_text(json.dumps({"enabled": "yes", "max_facts": -1, "max_chars": "big"}), encoding="utf-8")
    snap = RetrievalConfig(p).snapshot()
    assert snap == {"enabled": False, "max_facts": 4, "max_chars": 2400, "include_agents_md": False}


def test_chunk_text_splits_headings_and_long_blocks(tmp_path):
    text = "# Title\n\nIntro.\n\n## Section A\nA" + "b" * 3000
    chunks = _chunk_text(text)
    assert chunks[0].startswith("# Title")
    assert any("## Section A" in c for c in chunks)
    assert all(len(c) <= 1600 for c in chunks)


def test_remember_writes_project_fact(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    mem = RetrievalMemory(str(cwd), RetrievalConfig(tmp_path / "cfg.json"))
    result = mem.remember("Use pytest for tests.")
    assert "project memory" in result
    facts = list((cwd / ".cluxmate" / "memory" / "facts").glob("*.md"))
    assert len(facts) == 1
    assert "Use pytest" in facts[0].read_text("utf-8")


def test_remember_writes_global_fact(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    mem = RetrievalMemory(str(cwd), RetrievalConfig(tmp_path / "cfg.json"))
    mem.remember("Prefer terse replies.", scope="global")
    facts = list((home / ".cluxmate" / "memory" / "global" / "facts").glob("*.md"))
    assert len(facts) == 1


def test_remember_empty_content_is_error(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    mem = RetrievalMemory(str(cwd), RetrievalConfig(tmp_path / "cfg.json"))
    assert "Error" in mem.remember("   ")


def test_forget_deletes_fact_by_id(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    mem = RetrievalMemory(str(cwd), RetrievalConfig(tmp_path / "cfg.json"))
    result = mem.remember("temp fact")
    fact_id = result.split()[2]
    assert "Forgot" in mem.forget(fact_id)
    assert not list((cwd / ".cluxmate" / "memory" / "facts").glob("*.md"))


def test_forget_rejects_traversal(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    mem = RetrievalMemory(str(cwd), RetrievalConfig(tmp_path / "cfg.json"))
    assert "invalid fact id" in mem.forget("../etc")


def test_forget_rejects_drive_relative_id(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    mem = RetrievalMemory(str(cwd), RetrievalConfig(tmp_path / "cfg.json"))
    assert "invalid fact id" in mem.forget("C:deadbeef1234")


def _enabled_mem(tmp_path, monkeypatch, *, include_agents_md=False) -> RetrievalMemory:
    home = _home(tmp_path, monkeypatch)
    cwd = tmp_path / "proj"
    cwd.mkdir(exist_ok=True)
    cfg = tmp_path / "retrieval-memory.json"
    cfg.write_text(
        json.dumps({"enabled": True, "include_agents_md": include_agents_md}),
        encoding="utf-8",
    )
    return RetrievalMemory(str(cwd), RetrievalConfig(cfg))


def test_recall_disabled_returns_none(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    mem = RetrievalMemory(str(cwd), RetrievalConfig(tmp_path / "cfg.json"))
    mem.remember("Use pytest for tests.")
    assert mem.recall("what test framework should I use?") is None


def test_recall_generic_or_short_query_skipped(tmp_path, monkeypatch):
    mem = _enabled_mem(tmp_path, monkeypatch)
    mem.remember("Use pytest for tests.")
    assert mem.recall("ok") is None
    assert mem.recall("hi") is None


def test_recall_english_term_overlap(tmp_path, monkeypatch):
    mem = _enabled_mem(tmp_path, monkeypatch)
    mem.remember("Use pytest for tests.")
    mem.remember("Deploy with Docker.", scope="global")
    out = mem.recall("what test framework should I use?")
    assert out is not None
    assert "Use pytest" in out
    assert "Deploy with Docker" not in out


def test_recall_cjk_bigram(tmp_path, monkeypatch):
    mem = _enabled_mem(tmp_path, monkeypatch)
    mem.remember("数据库使用 B-tree 索引。")
    out = mem.recall("数据库索引怎么建")
    assert out is not None
    assert "B-tree" in out


def test_tokenize_cjk_emits_three_char_windows():
    # FTS5's trigram tokenizer needs >= 3 characters to MATCH, so a Chinese
    # query can only reach the index through 3-char windows.
    toks = _tokenize("数据库索引")
    assert "数据库" in toks
    assert "据库索" in toks
    assert "库索引" in toks


def test_tokenize_keeps_cjk_bigrams():
    # Bigrams stay: they cover 2-char words and non-adjacent matches that no
    # query-side trigram can reach.
    assert "数据" in _tokenize("数据库")
    assert "缓存" in _tokenize("缓存")


def test_recall_cjk_terms_reach_the_fts_index(tmp_path, monkeypatch):
    mem = _enabled_mem(tmp_path, monkeypatch)
    mem.remember("数据库使用 B-tree 索引。")
    seen: list[tuple[str, int]] = []
    original = mem._fts_match

    def spy(term: str):
        hits = original(term)
        seen.append((term, len(hits)))
        return hits

    monkeypatch.setattr(mem, "_fts_match", spy)
    out = mem.recall("数据库索引怎么建")
    assert out is not None
    assert "B-tree" in out
    cjk = [t for t, _ in seen if len(t) >= 3 and all("\u4e00" <= c <= "\u9fff" for c in t)]
    assert cjk, f"no CJK term reached the FTS index; saw {seen}"
    assert any(n for t, n in seen if t in cjk), "the index returned no CJK match"


def test_recall_short_cjk_word_still_matches(tmp_path, monkeypatch):
    # A 2-char word that never forms a query-side trigram with its neighbours
    # must still be recalled -- trigram-only tokenizing would miss this.
    mem = _enabled_mem(tmp_path, monkeypatch)
    mem.remember("用 Redis 做缓存。")
    out = mem.recall("缓存怎么实现")
    assert out is not None
    assert "Redis" in out


def test_recall_respects_max_facts(tmp_path, monkeypatch):
    mem = _enabled_mem(tmp_path, monkeypatch)
    for i in range(6):
        mem.remember(f"fact number {i} about testing.")
    out = mem.recall("testing facts")
    assert out is not None
    # At most the default max_facts (4) entries.
    assert out.count("- [") <= 4


def test_recall_reconciles_new_fact(tmp_path, monkeypatch):
    mem = _enabled_mem(tmp_path, monkeypatch)
    assert mem.recall("pytest testing") is None
    mem.remember("Use pytest for tests.")
    assert mem.recall("pytest testing") is not None


def test_recall_from_other_thread_than_constructor(tmp_path, monkeypatch):
    # Regression: the JSON-RPC server lazily builds RetrievalMemory on the
    # initialize/MCP-loader thread but runs every chat turn (and so recall())
    # on its own fresh worker thread. sqlite3 connections are thread-bound by
    # default, so cross-thread recall used to raise ProgrammingError.
    mem = _enabled_mem(tmp_path, monkeypatch)
    mem.remember("Use pytest for tests.")
    out: list[str | None] = []
    error: list[BaseException] = []

    def worker():
        try:
            out.append(mem.recall("what test framework should I use?"))
        except BaseException as exc:  # pragma: no cover - asserted below
            error.append(exc)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert error == []
    assert out and "Use pytest" in out[0]


def test_recall_agents_md_when_enabled(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    (cwd / "AGENTS.md").write_text("# Project\n\n## Testing\nRun pytest.", encoding="utf-8")
    mem = _enabled_mem(tmp_path, monkeypatch, include_agents_md=True)
    out = mem.recall("how to run tests")
    assert out is not None
    assert "pytest" in out


def test_recall_agents_md_returns_multiple_matching_chunks(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    (cwd / "AGENTS.md").write_text(
        "# P\n\n## A\nrun pytest for unit tests\n\n## B\nrun pytest for integration tests",
        encoding="utf-8",
    )
    mem = _enabled_mem(tmp_path, monkeypatch, include_agents_md=True)
    out = mem.recall("run pytest tests")
    assert out is not None
    assert "unit tests" in out
    assert "integration tests" in out


def test_set_enabled_persists(tmp_path):
    p = tmp_path / "retrieval-memory.json"
    cfg = RetrievalConfig(p)
    snap = cfg.set_enabled(True)
    assert snap["enabled"] is True
    assert snap["max_facts"] == 4
    assert RetrievalConfig(p).snapshot()["enabled"] is True


def test_set_enabled_preserves_other_fields(tmp_path):
    p = tmp_path / "retrieval-memory.json"
    p.write_text(
        json.dumps({"enabled": False, "max_facts": 9, "max_chars": 100, "include_agents_md": True}),
        encoding="utf-8",
    )
    cfg = RetrievalConfig(p)
    snap = cfg.set_enabled(True)
    assert snap == {"enabled": True, "max_facts": 9, "max_chars": 100, "include_agents_md": True}


def test_set_enabled_creates_file_with_defaults(tmp_path):
    p = tmp_path / "retrieval-memory.json"
    RetrievalConfig(p).set_enabled(True)
    data = json.loads(p.read_text("utf-8"))
    assert data == {"enabled": True, "max_facts": 4, "max_chars": 2400, "include_agents_md": False}


def test_set_enabled_rejects_non_bool(tmp_path):
    cfg = RetrievalConfig(tmp_path / "retrieval-memory.json")
    with pytest.raises(ValueError):
        cfg.set_enabled("yes")
