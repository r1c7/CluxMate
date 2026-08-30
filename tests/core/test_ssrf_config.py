"""Tests for the SSRF network-access config store (ssrf.json)."""

import json

import pytest

from cluxmate.core.ssrf_config import SsrConfig


def test_missing_file_yields_empty(tmp_path):
    cfg = SsrConfig(path=tmp_path / "ssrf.json")
    assert cfg.snapshot() == {"allow": [], "block_extra": []}


def test_set_rules_persists_across_instances(tmp_path):
    p = tmp_path / "ssrf.json"
    cfg = SsrConfig(path=p)
    cfg.set_rules(allow=["localhost:3000", "10.0.0.0/8"], block_extra=["203.0.113.0/24"])
    reloaded = SsrConfig(path=p)
    assert reloaded.snapshot() == {
        "allow": ["localhost:3000", "10.0.0.0/8"],
        "block_extra": ["203.0.113.0/24"],
    }


def test_set_rules_rejects_invalid(tmp_path):
    cfg = SsrConfig(path=tmp_path / "ssrf.json")
    with pytest.raises(ValueError):
        cfg.set_rules(allow=["bad entry with spaces"], block_extra=[])
    # nothing was written
    assert SsrConfig(path=tmp_path / "ssrf.json").snapshot() == {"allow": [], "block_extra": []}


def test_corrupt_file_yields_empty(tmp_path):
    p = tmp_path / "ssrf.json"
    p.write_text("{not json", encoding="utf-8")
    assert SsrConfig(path=p).snapshot() == {"allow": [], "block_extra": []}


def test_non_list_fields_ignored(tmp_path):
    p = tmp_path / "ssrf.json"
    p.write_text(json.dumps({"allow": "nope", "block_extra": ["10.0.0.0/8"]}), encoding="utf-8")
    assert SsrConfig(path=p).snapshot() == {"allow": [], "block_extra": ["10.0.0.0/8"]}


def test_external_edit_is_picked_up(tmp_path):
    """mtime cache: a file changed out-of-band (desktop Settings writes it
    directly) is re-read on the next snapshot."""
    p = tmp_path / "ssrf.json"
    cfg = SsrConfig(path=p)
    assert cfg.snapshot() == {"allow": [], "block_extra": []}
    p.write_text(json.dumps({"allow": ["127.0.0.1:3000"], "block_extra": []}), encoding="utf-8")
    assert cfg.snapshot() == {"allow": ["127.0.0.1:3000"], "block_extra": []}
