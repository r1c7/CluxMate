"""Project trust registry — decisions, normalization, and the per-run override."""

import json
import os
from pathlib import Path

import pytest

from cluxmate.core.trust import (
    DENIED,
    TRUSTED,
    UNKNOWN,
    TrustStore,
    canonical,
    resolve_trust,
)


def _store(tmp_path: Path) -> TrustStore:
    return TrustStore(tmp_path / "home" / ".cluxmate" / "trust.json")


def test_unknown_for_unregistered_directory(tmp_path):
    store = _store(tmp_path)
    assert store.status(str(tmp_path)) == UNKNOWN
    decision = resolve_trust(str(tmp_path), store)
    assert decision.source == "default"
    assert decision.trusted is False
    assert decision.pending is False  # no project config in an empty dir


def test_set_persists_a_versioned_registry(tmp_path):
    store = _store(tmp_path)
    target = tmp_path / "proj"
    target.mkdir()
    store.set(str(target), TRUSTED)
    raw = json.loads(store._path.read_text("utf-8"))
    assert raw["version"] == 1
    assert raw["folders"] == {canonical(str(target)): TRUSTED}
    assert TrustStore(store._path).status(str(target)) == TRUSTED


def test_denied_is_remembered(tmp_path):
    store = _store(tmp_path)
    store.set(str(tmp_path), DENIED)
    assert store.status(str(tmp_path)) == DENIED
    assert resolve_trust(str(tmp_path), store).source == "registry"


def test_lookup_normalizes_trailing_separator_and_case(tmp_path):
    store = _store(tmp_path)
    target = tmp_path / "proj"
    target.mkdir()
    store.set(str(target), TRUSTED)
    assert store.status(str(target) + os.sep) == TRUSTED
    assert store.status(str(target) + os.sep + ".") == TRUSTED
    if os.name == "nt":
        assert store.status(str(target).upper()) == TRUSTED


def test_invalid_status_is_rejected(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.set(str(tmp_path), "maybe")
    with pytest.raises(ValueError):
        store.set_session(str(tmp_path), "maybe")


def test_corrupt_registry_is_tolerated(tmp_path):
    path = tmp_path / "trust.json"
    path.write_text("{not json", encoding="utf-8")
    assert TrustStore(path).entries() == {}
    path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert TrustStore(path).entries() == {}
    path.write_text(json.dumps({"folders": ["nope"]}), encoding="utf-8")
    assert TrustStore(path).entries() == {}


def test_unknown_status_values_are_dropped(tmp_path):
    path = tmp_path / "trust.json"
    target = tmp_path / "proj"
    target.mkdir()
    path.write_text(
        json.dumps({"version": 1, "folders": {canonical(str(target)): "whatever"}}),
        encoding="utf-8",
    )
    assert TrustStore(path).status(str(target)) == UNKNOWN


def test_session_override_wins_and_never_touches_disk(tmp_path):
    path = tmp_path / "home" / ".cluxmate" / "trust.json"
    store = TrustStore(path)
    target = tmp_path / "proj"
    target.mkdir()
    store.set_session(str(target), TRUSTED)
    assert store.status(str(target)) == TRUSTED
    assert resolve_trust(str(target), store).source == "session"
    assert not path.exists()
    assert TrustStore(path).status(str(target)) == UNKNOWN


def test_session_override_beats_a_registry_denial(tmp_path):
    store = _store(tmp_path)
    store.set(str(tmp_path), DENIED)
    store.set_session(str(tmp_path), TRUSTED)
    assert store.status(str(tmp_path)) == TRUSTED
    assert resolve_trust(str(tmp_path), store).source == "session"


def test_remove_clears_the_entry(tmp_path):
    store = _store(tmp_path)
    store.set(str(tmp_path), TRUSTED)
    assert store.remove(str(tmp_path)) is True
    assert store.status(str(tmp_path)) == UNKNOWN
    assert store.remove(str(tmp_path)) is False


def test_entries_returns_a_copy(tmp_path):
    store = _store(tmp_path)
    store.set(str(tmp_path), TRUSTED)
    snapshot = store.entries()
    snapshot.clear()
    assert store.entries()


def test_symlinked_path_resolves_to_the_same_key(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")
    store = _store(tmp_path)
    store.set(str(real), TRUSTED)
    assert store.status(str(link)) == TRUSTED
