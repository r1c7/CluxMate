"""Tests for the writable-folder grant registry (sandbox-grants.json)."""

import json
from pathlib import Path

from cluxmate.core.grants import GrantStore


def test_add_and_snapshot(tmp_path):
    store = GrantStore(root=tmp_path)
    p = str(tmp_path / "data")
    normalized = store.add(p)
    assert Path(normalized).is_absolute()
    assert store.snapshot() == [normalized]


def test_add_is_idempotent(tmp_path):
    store = GrantStore(root=tmp_path)
    a = store.add(str(tmp_path / "data"))
    b = store.add(str(tmp_path / "data"))
    assert a == b
    assert len(store.snapshot()) == 1


def test_add_resolves_relative(tmp_path, monkeypatch):
    store = GrantStore(root=tmp_path)
    monkeypatch.chdir(tmp_path)
    normalized = store.add("sub")
    assert Path(normalized) == (tmp_path / "sub").resolve()


def test_remove_returns_removed_path(tmp_path):
    store = GrantStore(root=tmp_path)
    p = store.add(str(tmp_path / "data"))
    assert store.remove(p) == p
    assert store.snapshot() == []
    assert store.remove(p) is None  # already gone


def test_remove_by_equivalent_form(tmp_path):
    store = GrantStore(root=tmp_path)
    p = store.add(str(tmp_path / "data"))
    # A path that RESOLVES to the same directory (data/../data) removes it.
    assert store.remove(str(tmp_path / "data" / ".." / "data")) == p


def test_persists_across_instances(tmp_path):
    store = GrantStore(root=tmp_path)
    store.add(str(tmp_path / "a"))
    store.add(str(tmp_path / "b"))
    # New instance reads the same file.
    reloaded = GrantStore(root=tmp_path)
    assert sorted(reloaded.snapshot()) == sorted([
        str((tmp_path / "a").resolve()),
        str((tmp_path / "b").resolve()),
    ])


def test_corrupt_file_yields_empty(tmp_path):
    (tmp_path / "sandbox-grants.json").write_text("{not json", encoding="utf-8")
    store = GrantStore(root=tmp_path)
    assert store.snapshot() == []


def test_non_list_paths_ignored(tmp_path):
    (tmp_path / "sandbox-grants.json").write_text(
        json.dumps({"paths": "nope"}), encoding="utf-8"
    )
    store = GrantStore(root=tmp_path)
    assert store.snapshot() == []
