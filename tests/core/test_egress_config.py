"""Tests for the network-egress config store (egress.json)."""

import json

import pytest

from cluxmate.core.egress_config import EgressConfig


def test_missing_file_yields_shared(tmp_path):
    assert EgressConfig(path=tmp_path / "egress.json").snapshot() == {"mode": "shared"}


def test_set_mode_persists_across_instances(tmp_path):
    p = tmp_path / "egress.json"
    cfg = EgressConfig(path=p)
    cfg.set_mode("off")
    assert EgressConfig(path=p).snapshot() == {"mode": "off"}


def test_set_mode_rejects_invalid(tmp_path):
    cfg = EgressConfig(path=tmp_path / "egress.json")
    with pytest.raises(ValueError):
        cfg.set_mode("bogus")
    # nothing was written
    assert EgressConfig(path=tmp_path / "egress.json").snapshot() == {"mode": "shared"}


def test_corrupt_file_yields_shared(tmp_path):
    p = tmp_path / "egress.json"
    p.write_text("{not json", encoding="utf-8")
    assert EgressConfig(path=p).snapshot() == {"mode": "shared"}


def test_invalid_mode_in_file_falls_back_to_shared(tmp_path):
    p = tmp_path / "egress.json"
    p.write_text(json.dumps({"mode": "bogus"}), encoding="utf-8")
    assert EgressConfig(path=p).snapshot() == {"mode": "shared"}


def test_external_edit_is_picked_up(tmp_path):
    p = tmp_path / "egress.json"
    cfg = EgressConfig(path=p)
    assert cfg.snapshot() == {"mode": "shared"}
    p.write_text(json.dumps({"mode": "proxy"}), encoding="utf-8")
    assert cfg.snapshot() == {"mode": "proxy"}
