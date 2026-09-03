"""Tests for the read-denylist registry (forbid-read.json)."""

import json
import os
from pathlib import Path

from cluxmate.core.read_denies import (
    ReadDenyStore,
    is_sensitive_pattern,
    sensitive_dir_defaults,
)


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


# ---------------------------------------------------------------------------
# Built-in sensitive-file template (protect_sensitive)
# ---------------------------------------------------------------------------

def test_protect_sensitive_defaults_off(tmp_path):
    """Zero behavior change: the toggle is off until the user enables it."""
    store = ReadDenyStore(root=tmp_path)
    assert store.protect_sensitive() is False
    assert store.effective_paths() == []


def test_legacy_file_without_toggle_defaults_off(tmp_path):
    (tmp_path / "forbid-read.json").write_text(
        json.dumps({"paths": []}), encoding="utf-8"
    )
    store = ReadDenyStore(root=tmp_path)
    assert store.protect_sensitive() is False


def test_set_protect_sensitive_persists(tmp_path):
    store = ReadDenyStore(root=tmp_path)
    assert store.set_protect_sensitive(True) is True
    assert store.protect_sensitive() is True
    reloaded = ReadDenyStore(root=tmp_path)
    assert reloaded.protect_sensitive() is True
    assert store.set_protect_sensitive(False) is False
    assert ReadDenyStore(root=tmp_path).protect_sensitive() is False


def test_set_protect_sensitive_keeps_user_paths(tmp_path):
    store = ReadDenyStore(root=tmp_path)
    p = store.add(str(tmp_path / ".aws"))
    store.set_protect_sensitive(True)
    reloaded = ReadDenyStore(root=tmp_path)
    assert reloaded.protect_sensitive() is True
    assert reloaded.snapshot() == [p]


def test_effective_paths_gated_by_toggle(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    store = ReadDenyStore(root=tmp_path)
    user = store.add(str(tmp_path / "secret-folder"))
    assert store.effective_paths() == [user]  # off: user paths only
    store.set_protect_sensitive(True)
    effective = store.effective_paths()
    assert user in effective
    for d in sensitive_dir_defaults():
        assert d in effective


def test_sensitive_dir_defaults_platform_aware(tmp_path, monkeypatch):
    home = tmp_path / "home"
    appdata = tmp_path / "appdata"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("APPDATA", str(appdata))
    dirs = sensitive_dir_defaults()
    assert str(home / ".ssh") in dirs
    assert str(home / ".aws") in dirs
    if os.name == "nt":
        assert str(appdata / "gnupg") in dirs
    else:
        assert str(home / ".gnupg") in dirs


def test_is_sensitive_pattern_basename_and_suffix(tmp_path):
    assert is_sensitive_pattern(tmp_path / "proj" / ".env")
    assert is_sensitive_pattern(tmp_path / ".git-credentials")
    assert is_sensitive_pattern(tmp_path / ".netrc")
    assert is_sensitive_pattern(tmp_path / "server.pem")
    assert is_sensitive_pattern(tmp_path / "id_rsa.key")
    assert is_sensitive_pattern(tmp_path / "cert.p12")
    assert is_sensitive_pattern(tmp_path / "bundle.pfx")
    assert is_sensitive_pattern(tmp_path / ".ENV")  # case-insensitive
    assert not is_sensitive_pattern(tmp_path / ".env.production")
    assert not is_sensitive_pattern(tmp_path / "app.env")
    assert not is_sensitive_pattern(tmp_path / "monkey.txt")
