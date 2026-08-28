"""Tests for the read-denylist registry (forbid-read.json)."""

import json
from pathlib import Path

from cluxmate.core.read_denies import ReadDenyStore


def test_add_and_snapshot(tmp_path):
    store = ReadDenyStore(root=tmp_path)
    p = str(tmp_path / ".ssh")
    normalized = store.add(p)
    assert Path(normalized).is_absolute()
    assert store.snapshot() == [normalized]


def test_add_is_idempotent(tmp_path):
    store = ReadDenyStore(root=tmp_path)
    a = store.add(str(tmp_path / ".ssh"))
    b = store.add(str(tmp_path / ".ssh"))
    assert a == b
    assert len(store.snapshot()) == 1


def test_add_resolves_relative(tmp_path, monkeypatch):
    store = ReadDenyStore(root=tmp_path)
    monkeypatch.chdir(tmp_path)
    normalized = store.add("sub")
    assert Path(normalized) == (tmp_path / "sub").resolve()


def test_remove_returns_removed_path(tmp_path):
    store = ReadDenyStore(root=tmp_path)
    p = store.add(str(tmp_path / ".ssh"))
    assert store.remove(p) == p
    assert store.snapshot() == []
    assert store.remove(p) is None  # already gone


def test_remove_by_equivalent_form(tmp_path):
    store = ReadDenyStore(root=tmp_path)
    p = store.add(str(tmp_path / ".ssh"))
    assert store.remove(str(tmp_path / ".ssh" / ".." / ".ssh")) == p


def test_persists_across_instances(tmp_path):
    store = ReadDenyStore(root=tmp_path)
    store.add(str(tmp_path / ".ssh"))
    store.add(str(tmp_path / ".aws"))
    reloaded = ReadDenyStore(root=tmp_path)
    assert sorted(reloaded.snapshot()) == sorted([
        str((tmp_path / ".ssh").resolve()),
        str((tmp_path / ".aws").resolve()),
    ])


def test_corrupt_file_yields_empty(tmp_path):
    (tmp_path / "forbid-read.json").write_text("{not json", encoding="utf-8")
    store = ReadDenyStore(root=tmp_path)
    assert store.snapshot() == []


def test_non_list_paths_ignored(tmp_path):
    (tmp_path / "forbid-read.json").write_text(
        json.dumps({"paths": "nope"}), encoding="utf-8"
    )
    store = ReadDenyStore(root=tmp_path)
    assert store.snapshot() == []


def test_empty_store_is_empty_default(tmp_path):
    store = ReadDenyStore(root=tmp_path)
    assert store.snapshot() == []
