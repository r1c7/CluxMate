"""Tests for retrieval-memory config, fact store, chunking, and recall."""

import json
from pathlib import Path

from cluxmate.core.retrieval_memory import (
    RetrievalConfig,
    RetrievalMemory,
    _chunk_text,
)


def _home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True)
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
