"""Tests for CheckpointManager (shadow-git workspace snapshots)."""

import hashlib
import shutil
from pathlib import Path

import pytest

import cluxmate.core.checkpoints as ckpt
from cluxmate.core.checkpoints import CheckpointManager

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not on PATH"
)


def _mk(tmp_path: Path) -> CheckpointManager:
    """A manager whose shadow repo lives under tmp_path (not ~/.cluxmate)."""
    work = tmp_path / "work"
    work.mkdir()
    mgr = CheckpointManager(str(work))
    mgr._shadow_dir = str(tmp_path / "shadow.git")
    return mgr, work


def test_available_and_init(tmp_path):
    mgr, _ = _mk(tmp_path)
    assert mgr.available() is True
    assert mgr.ensure_init() is True
    assert Path(mgr._shadow_dir).exists()
    # .git/ and .cluxmate/ must be excluded so neither the user's real repo nor
    # CluxMate's own per-project state is captured.
    exclude = (Path(mgr._shadow_dir) / "info" / "exclude").read_text("utf-8")
    assert ".git/" in exclude
    assert ".cluxmate/" in exclude


def test_snapshot_and_list(tmp_path):
    mgr, work = _mk(tmp_path)
    (work / "a.txt").write_text("one", encoding="utf-8")
    sha1 = mgr.snapshot("sess1", "first")
    assert sha1

    (work / "a.txt").write_text("two", encoding="utf-8")
    sha2 = mgr.snapshot("sess1", "second")
    assert sha2 and sha2 != sha1

    # A snapshot for a different session must not appear in sess1's list.
    (work / "b.txt").write_text("x", encoding="utf-8")
    mgr.snapshot("sess2", "other")

    items = mgr.list("sess1")
    assert [i["label"] for i in items] == ["second", "first"]  # newest first
    assert all(i["id"] for i in items)


def test_git_dir_not_captured(tmp_path):
    """A real .git inside the work-tree must never enter a snapshot."""
    mgr, work = _mk(tmp_path)
    real_git = work / ".git"
    real_git.mkdir()
    (real_git / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")
    (work / "code.py").write_text("print(1)", encoding="utf-8")
    sha = mgr.snapshot("s", "snap")
    files = mgr.diff(sha)
    paths = {f["path"] for f in files}
    assert "code.py" in paths
    assert not any(p.startswith(".git/") for p in paths)


def test_cluxmate_dir_not_captured(tmp_path):
    """CluxMate's own <cwd>/.cluxmate state (permissions.json etc.) must never
    enter a snapshot — otherwise a UI toggle would pollute the turn diff and undo
    could revert it."""
    mgr, work = _mk(tmp_path)
    af = work / ".cluxmate"
    af.mkdir()
    (af / "permissions.json").write_text('{"accept_edits": true}', encoding="utf-8")
    (work / "code.py").write_text("print(1)", encoding="utf-8")
    sha = mgr.snapshot("s", "snap")
    paths = {f["path"] for f in mgr.diff(sha)}
    assert "code.py" in paths
    assert not any(p.startswith(".cluxmate/") for p in paths)


def test_exclude_upgrade_preserves_existing(tmp_path):
    """An existing shadow repo that only excluded .git/ gains .cluxmate/ on the
    next ensure_init without losing the original pattern."""
    mgr, _ = _mk(tmp_path)
    # Simulate a pre-upgrade repo: init, then rewrite exclude with only .git/.
    mgr.ensure_init()
    exclude = Path(mgr._shadow_dir) / "info" / "exclude"
    exclude.write_text(".git/\n", encoding="utf-8")
    mgr._initialized = False  # force re-run of the exclude logic
    mgr.ensure_init()
    content = exclude.read_text("utf-8")
    assert ".git/" in content
    assert ".cluxmate/" in content


def test_diff_add_modify_delete(tmp_path):
    mgr, work = _mk(tmp_path)
    (work / "keep.txt").write_text("v1", encoding="utf-8")
    (work / "gone.txt").write_text("bye", encoding="utf-8")
    mgr.snapshot("s", "base")

    (work / "keep.txt").write_text("v2", encoding="utf-8")   # M
    (work / "new.txt").write_text("hi", encoding="utf-8")     # A
    (work / "gone.txt").unlink()                              # D
    sha = mgr.snapshot("s", "changed")

    by_path = {f["path"]: f for f in mgr.diff(sha)}
    assert by_path["keep.txt"]["status"] == "M"
    assert by_path["keep.txt"]["old_content"] == "v1"
    assert by_path["keep.txt"]["new_content"] == "v2"
    assert by_path["new.txt"]["status"] == "A"
    assert by_path["new.txt"]["old_content"] == ""
    assert by_path["new.txt"]["new_content"] == "hi"
    assert by_path["gone.txt"]["status"] == "D"
    assert by_path["gone.txt"]["old_content"] == "bye"


def test_summary_counts(tmp_path):
    mgr, work = _mk(tmp_path)
    (work / "keep.txt").write_text("a\nb\nc\n", encoding="utf-8")
    (work / "gone.txt").write_text("x\n", encoding="utf-8")
    mgr.snapshot("s", "base")

    (work / "keep.txt").write_text("a\nB\nc\nd\n", encoding="utf-8")  # +2 -1
    (work / "new.txt").write_text("n1\nn2\n", encoding="utf-8")        # A +2
    (work / "gone.txt").unlink()                                      # D -1
    sha = mgr.snapshot("s", "changed")

    by_path = {f["path"]: f for f in mgr.summary(sha)}
    assert by_path["keep.txt"]["status"] == "M"
    assert by_path["keep.txt"]["additions"] == 2
    assert by_path["keep.txt"]["deletions"] == 1
    assert by_path["new.txt"]["status"] == "A"
    assert by_path["new.txt"]["additions"] == 2
    assert by_path["gone.txt"]["status"] == "D"
    assert by_path["gone.txt"]["deletions"] == 1


def test_restore_only_touches_changed_files(tmp_path):
    mgr, work = _mk(tmp_path)
    (work / "agent.txt").write_text("original", encoding="utf-8")
    (work / "manual.txt").write_text("user-original", encoding="utf-8")
    base = mgr.snapshot("s", "base")

    # Agent modifies agent.txt and creates created.txt.
    (work / "agent.txt").write_text("agent-changed", encoding="utf-8")
    (work / "created.txt").write_text("new", encoding="utf-8")
    mgr.snapshot("s", "after agent")

    # Meanwhile the user hand-edits manual.txt AFTER the base checkpoint.
    (work / "manual.txt").write_text("user-edited-later", encoding="utf-8")

    result = mgr.restore(base, "s")

    # agent.txt reverts, created.txt is removed...
    assert (work / "agent.txt").read_text("utf-8") == "original"
    assert not (work / "created.txt").exists()
    assert "agent.txt" in result["restored"]
    assert "created.txt" in result["deleted"]
    # ...but the user's later manual edit is preserved (not clobbered).
    assert (work / "manual.txt").read_text("utf-8") == "user-edited-later"


def test_restore_does_not_revert_other_session(tmp_path):
    """The shadow repo is shared across sessions in a working dir. Restoring one
    session's checkpoint must not revert files another session changed in the
    interleaved shared history."""
    mgr, work = _mk(tmp_path)
    (work / "file1.txt").write_text("a1", encoding="utf-8")
    (work / "file3.txt").write_text("a3", encoding="utf-8")
    A = mgr.snapshot("session1", "A")

    (work / "file1.txt").write_text("b1", encoding="utf-8")
    mgr.snapshot("session1", "B")

    # session2 works in the SAME directory: modifies file3, creates file2.
    (work / "file3.txt").write_text("s2-modified", encoding="utf-8")
    (work / "file2.txt").write_text("s2-new", encoding="utf-8")
    mgr.snapshot("session2", "s2")

    result = mgr.restore(A, "session1")

    # session1's own file reverts to A...
    assert (work / "file1.txt").read_text("utf-8") == "a1"
    assert result["restored"] == ["file1.txt"]
    # ...but session2's modification and new file are untouched.
    assert (work / "file3.txt").read_text("utf-8") == "s2-modified"
    assert (work / "file2.txt").read_text("utf-8") == "s2-new"


def test_repeated_restore_keeps_other_session_intact(tmp_path):
    """Restoring twice must still not absorb/revert another session's work — the
    before/after-restore snapshots are path-scoped, so session2's on-disk files
    never enter session1's history."""
    mgr, work = _mk(tmp_path)
    (work / "file1.txt").write_text("a1", encoding="utf-8")
    A = mgr.snapshot("session1", "A")
    (work / "file1.txt").write_text("b1", encoding="utf-8")
    mgr.snapshot("session1", "B")

    (work / "file3.txt").write_text("s2-modified", encoding="utf-8")
    (work / "file2.txt").write_text("s2-new", encoding="utf-8")
    mgr.snapshot("session2", "s2")

    mgr.restore(A, "session1")
    mgr.restore(A, "session1")  # second rewind

    assert (work / "file3.txt").read_text("utf-8") == "s2-modified"
    assert (work / "file2.txt").read_text("utf-8") == "s2-new"


def test_restore_reports_cross_session_conflict(tmp_path):
    """When another session also edited a file this session touched, the rewind
    still applies but the path is flagged in `conflicts`."""
    mgr, work = _mk(tmp_path)
    (work / "shared.txt").write_text("v0", encoding="utf-8")
    A = mgr.snapshot("s1", "A")
    (work / "shared.txt").write_text("s1-edit", encoding="utf-8")
    mgr.snapshot("s1", "B")
    (work / "shared.txt").write_text("s2-edit", encoding="utf-8")
    mgr.snapshot("s2", "s2work")

    result = mgr.restore(A, "s1")
    assert result.get("conflicts") == ["shared.txt"]


def test_restore_flags_uncommitted_disk_drift(tmp_path):
    """A conflict must be flagged even when the competing edit is only on disk —
    not yet snapshotted. Checkpoints fire at chat/send boundaries, so another
    session's mid-turn edit lives on disk before its own checkpoint exists; a
    rewind would silently overwrite it, so `conflicts` must catch it."""
    mgr, work = _mk(tmp_path)
    f = work / "shared.txt"
    f.write_text("A", encoding="utf-8")
    pre = mgr.snapshot("session1", "pre s1")
    f.write_text("B", encoding="utf-8")
    mgr.snapshot("session1", "post s1")

    # Another session edits the same file on disk but NO snapshot is taken yet.
    f.write_text("C", encoding="utf-8")

    result = mgr.restore(pre, "session1")
    assert result.get("conflicts") == ["shared.txt"]
    # The clobbered on-disk "C" survives in history (before-restore snapshot).
    assert any(
        mgr._run("show", f"{cp['id']}:shared.txt").stdout == "C"
        for cp in mgr.list("session1")
    )


def test_restore_no_conflict_for_drift_on_untouched_file(tmp_path):
    """On-disk drift to a file THIS session never touched is not a conflict and
    must be left alone (it's another session's / the user's file)."""
    mgr, work = _mk(tmp_path)
    (work / "f1.txt").write_text("a1", encoding="utf-8")
    A = mgr.snapshot("s1", "A")
    (work / "f1.txt").write_text("b1", encoding="utf-8")
    mgr.snapshot("s1", "B")
    # Unsnapshotted edit to a different file.
    (work / "f2.txt").write_text("other-session", encoding="utf-8")

    result = mgr.restore(A, "s1")
    assert "conflicts" not in result
    assert (work / "f2.txt").read_text("utf-8") == "other-session"


def test_unavailable_is_noop(tmp_path, monkeypatch):
    mgr, _ = _mk(tmp_path)
    monkeypatch.setattr(mgr, "_git", None)
    assert mgr.available() is False
    assert mgr.ensure_init() is False
    assert mgr.snapshot("s", "x") is None
    assert mgr.list("s") == []
    assert mgr.diff("deadbeef") == []
    assert mgr.restore("deadbeef", "s")["available"] is False


def test_snapshot_skips_oversized_file(tmp_path, monkeypatch):
    """A file over the size cap must never enter a snapshot blob, while its
    siblings are still captured."""
    monkeypatch.setattr(ckpt, "_MAX_FILE_BYTES", 1024)
    mgr, work = _mk(tmp_path)
    (work / "small.txt").write_text("small", encoding="utf-8")
    (work / "big.bin").write_bytes(b"\0" * 2048)
    sha = mgr.snapshot("s", "snap")
    assert sha
    paths = {f["path"] for f in mgr.diff(sha)}
    assert "small.txt" in paths
    assert "big.bin" not in paths
    # The file stays on disk — only the snapshot omits it.
    assert (work / "big.bin").exists()


def test_snapshot_keeps_prior_version_when_file_grows_past_cap(tmp_path, monkeypatch):
    """A file already tracked (under the cap) that later grows past it should
    keep its last under-cap version, not re-record the oversized blob."""
    monkeypatch.setattr(ckpt, "_MAX_FILE_BYTES", 1024)
    mgr, work = _mk(tmp_path)
    (work / "big.bin").write_bytes(b"small-under-cap")
    base = mgr.snapshot("s", "base")
    assert base

    (work / "big.bin").write_bytes(b"\0" * 2048)
    sha = mgr.snapshot("s", "grown")
    assert sha
    # The grown commit must not record a new oversized blob — its stored
    # content is still the last under-cap version.
    content = mgr._run("show", f"{sha}:big.bin").stdout
    assert content == "small-under-cap"


def test_commit_cap_shallow_boundary(tmp_path, monkeypatch):
    """History past _MAX_COMMITS is dropped via a shallow boundary so list()
    sees only the newest N and subsequent snapshots still work."""
    monkeypatch.setattr(ckpt, "_MAX_COMMITS", 3)
    mgr, work = _mk(tmp_path)
    for i in range(5):
        (work / "f.txt").write_text(f"v{i}", encoding="utf-8")
        mgr.snapshot("s", f"c{i}")

    mgr._cap_commits()
    # Only 3 commits remain reachable.
    count = mgr._run("rev-list", "--count", "HEAD").stdout.strip()
    assert count == "3"
    # list() reflects the shortened history.
    assert len(mgr.list("s")) == 3
    # A subsequent snapshot still works and grows from the boundary.
    (work / "f.txt").write_text("after", encoding="utf-8")
    assert mgr.snapshot("s", "post")


def test_evict_stale_repos_deletes_only_stale(tmp_path, monkeypatch):
    """Sibling repos older than the retention window are deleted; fresh ones and
    our own repo are kept."""
    monkeypatch.setattr(ckpt, "_RETENTION_DAYS", 30)
    mgr, work = _mk(tmp_path)
    mgr.ensure_init()  # creates our own shadow repo under tmp_path

    root = tmp_path
    # A sibling repo we fake with an old mtime via git commit + env override is
    # complex; instead build a real sibling and backdate its commit via
    # GIT_COMMITTER_DATE, then let eviction read %ct (committer time).
    stale = tmp_path / "stale.git"
    sub = CheckpointManager(str(tmp_path / "stale-work"))
    sub._shadow_dir = str(stale)
    sub._cwd = str(tmp_path / "stale-work")
    (tmp_path / "stale-work").mkdir()
    sub.ensure_init()
    # Commit with a committer date well past the retention window.
    (tmp_path / "stale-work" / "x.txt").write_text("x", encoding="utf-8")
    env = sub._env()
    old = "2000-01-01T00:00:00+0000"
    env["GIT_COMMITTER_DATE"] = old
    env["GIT_AUTHOR_DATE"] = old
    sub._run("add", "-A", env=env)
    sub._run("commit", "-m", "old", env=env)

    mgr._evict_stale_repos()

    assert not stale.exists()          # stale sibling evicted
    assert Path(mgr._shadow_dir).exists()  # our own repo kept


def test_delete_shadow_repo_for_cwd(tmp_path):
    """The cwd-keyed purge deletes an existing repo and no-ops on absence."""
    work = tmp_path / "work"
    work.mkdir()
    root = tmp_path / "checkpoints"
    digest = hashlib.sha1(str(work.resolve()).encode("utf-8")).hexdigest()
    repo = root / f"{digest}.git"
    repo.mkdir(parents=True)
    (repo / "HEAD").write_text("x", encoding="utf-8")

    assert ckpt.delete_shadow_repo_for_cwd(str(work), checkpoints_root=root) is True
    assert not repo.exists()
    # A second call finds nothing and reports False.
    assert ckpt.delete_shadow_repo_for_cwd(str(work), checkpoints_root=root) is False
