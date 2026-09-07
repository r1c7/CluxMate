"""Tests for retrieval-memory config, fact store, chunking, and recall."""

import json
from pathlib import Path

from cluxmate.core.retrieval_memory import (
    RetrievalConfig,
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
