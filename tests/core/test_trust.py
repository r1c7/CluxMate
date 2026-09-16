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


def test_clear_session_drops_the_override(tmp_path):
    store = _store(tmp_path)
    target = tmp_path / "proj"
    target.mkdir()
    store.set_session(str(target), TRUSTED)
    # A variant spelling of the same directory must clear it too.
    store.clear_session(str(target) + os.sep)
    assert store.session_status(str(target)) == UNKNOWN
    assert store.status(str(target)) == UNKNOWN
    store.clear_session(str(target))  # nothing recorded: a no-op, not a KeyError


def test_a_persisted_set_clears_an_earlier_override(tmp_path):
    store = _store(tmp_path)
    target = tmp_path / "proj"
    target.mkdir()
    store.set_session(str(target), TRUSTED)
    store.set(str(target), DENIED)
    store.clear_session(str(target))
    assert resolve_trust(str(target), store).status == DENIED
    assert resolve_trust(str(target), store).source == "registry"


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


def test_forward_slash_form_is_the_same_directory(tmp_path):
    store = _store(tmp_path)
    target = tmp_path / "proj"
    target.mkdir()
    store.set(str(target).replace(os.sep, "/"), TRUSTED)
    assert store.status(str(target)) == TRUSTED
    assert list(store.entries()) == [canonical(str(target))]


def test_set_reuses_a_case_variant_key(tmp_path):
    """A key already recorded in another casing is updated, not duplicated."""
    if os.name != "nt":
        pytest.skip("registry keys are only matched case-insensitively on Windows")
    target = tmp_path / "proj"
    target.mkdir()
    variant = canonical(str(target)).upper()
    assert variant != canonical(str(target))  # else the test would prove nothing
    path = tmp_path / "trust.json"
    path.write_text(
        json.dumps({"version": 1, "folders": {variant: DENIED}}), encoding="utf-8"
    )
    store = TrustStore(path)
    store.set(str(target), TRUSTED)
    assert store.entries() == {variant: TRUSTED}
    assert store.status(str(target)) == TRUSTED


def test_canonical_survives_a_symlink_loop(tmp_path):
    """`resolve()` raises RuntimeError on a loop — the probe must still answer."""
    loop = tmp_path / "loop"
    try:
        loop.symlink_to(loop, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")
    assert canonical(str(loop)) == str(loop.absolute())
    store = _store(tmp_path)
    assert store.status(str(loop)) == UNKNOWN
    assert resolve_trust(str(loop), store).status == UNKNOWN


def test_a_failed_write_is_logged_and_swallowed(tmp_path, capsys):
    """A home the registry cannot be written to must not break the caller."""
    path = tmp_path / "trust.json"
    path.mkdir()  # a directory squatting on the registry path fails every write
    target = tmp_path / "proj"
    target.mkdir()
    store = TrustStore(path)
    store.set(str(target), TRUSTED)  # must not raise
    assert store.status(str(target)) == TRUSTED  # the in-memory answer survives
    assert "Traceback" in capsys.readouterr().err
    assert TrustStore(path).entries() == {}


# ── another process writing the registry ─────────────────────────────────
# The store is process-global (RPC server, TUI, CLI) but the file is shared, so
# a `cluxmate trust deny <dir>` run in a second terminal has to reach a store
# that is already constructed.


def _rewrite_registry(path: Path, folders: dict[str, str], mtime_ns: int) -> None:
    """Write the registry the way a second process would.

    The mtime is set explicitly so the test never depends on how coarse the
    filesystem clock is between two writes in the same tick.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "folders": folders}), encoding="utf-8")
    os.utime(path, ns=(mtime_ns, mtime_ns))


def test_an_out_of_band_write_is_observed_without_a_new_store(tmp_path):
    path = tmp_path / "home" / ".cluxmate" / "trust.json"
    store = TrustStore(path)
    target = tmp_path / "proj"
    target.mkdir()
    key = canonical(str(target))
    assert store.status(str(target)) == UNKNOWN

    _rewrite_registry(path, {key: DENIED}, 1_500_000_000_000_000_000)
    assert store.registry_status(str(target)) == DENIED
    assert resolve_trust(str(target), store).status == DENIED
    assert store.entries() == {key: DENIED}

    # The reverse direction matters as much: a revocation elsewhere must drop
    # the answer this process is still holding, not just add new ones.
    _rewrite_registry(path, {}, 1_500_000_000_000_000_001)
    assert store.status(str(target)) == UNKNOWN
    assert store.entries() == {}


def test_an_out_of_band_write_leaves_a_session_override_on_top(tmp_path):
    path = tmp_path / "home" / ".cluxmate" / "trust.json"
    store = TrustStore(path)
    target = tmp_path / "proj"
    target.mkdir()
    store.set_session(str(target), TRUSTED)

    _rewrite_registry(
        path, {canonical(str(target)): DENIED}, 1_500_000_000_000_000_000
    )
    assert store.status(str(target)) == TRUSTED
    assert resolve_trust(str(target), store).source == "session"


def test_a_reread_keeps_the_answer_this_process_wrote(tmp_path):
    """Our own `set` must not be re-read away by the next lookup."""
    path = tmp_path / "home" / ".cluxmate" / "trust.json"
    store = TrustStore(path)
    target = tmp_path / "proj"
    target.mkdir()
    store.set(str(target), TRUSTED)
    assert store.registry_status(str(target)) == TRUSTED
    store.remove(str(target))
    assert store.registry_status(str(target)) == UNKNOWN
