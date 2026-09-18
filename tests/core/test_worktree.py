"""`core/worktree.py`：宿主侧建树/删树机制 + `cluxmate worktree` CLI 契约。

用真 git 产物（照 `tests/core/test_project_root_integration.py:27-55` 的 `_git` 手法，
但**不 import** 那个文件：本仓没有 conftest.py，每个测试文件自带最小 helper）。
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from cluxmate.core import project_root, worktree

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

# The repo root, so the `python -m cluxmate` child finds the package even when
# the test session is not running from an editable install.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(*args, cwd=None):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "-c", "init.defaultBranch=master", *args],
        cwd=cwd, check=True, capture_output=True,
    )


def _git_out(*args, cwd=None) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8",
    )
    return proc.stdout


def _repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", cwd=repo)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "init", cwd=repo)
    return repo


def _same(a, b) -> bool:
    """Path equality that survives case / separator / symlink differences."""
    return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(
        os.path.realpath(str(b))
    )


def _key(path) -> str:
    return os.path.normcase(os.path.realpath(str(path)))


def _cli(args: list[str], cwd) -> subprocess.CompletedProcess:
    """Run `python -m cluxmate worktree <args> --json` in `cwd`."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "cluxmate", "worktree", *args, "--json"],
        cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )


def _one_json_line(proc: subprocess.CompletedProcess) -> dict:
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"stdout must be exactly one line, got: {proc.stdout!r}"
    return json.loads(lines[0])


@pytest.fixture(autouse=True)
def _no_cache():
    """`project_root.resolve` caches per cwd; every test gets a fresh view."""
    project_root.clear_cache()
    yield
    project_root.clear_cache()


# --- slugify ---------------------------------------------------------------


def test_slugify_lowercases_and_collapses_non_alphanumerics():
    assert worktree.slugify("  My Feature/Branch!! v2  ") == "my-feature-branch-v2"
    assert worktree.slugify("Hello   World") == "hello-world"


def test_slugify_truncates_to_forty_characters():
    assert worktree.slugify("a" * 60) == "a" * 40
    assert len(worktree.slugify("ab " * 30)) == 40


def test_slugify_truncation_does_not_leave_a_trailing_dash():
    assert worktree.slugify("a" * 39 + " b") == "a" * 39


def test_slugify_returns_empty_when_nothing_alphanumeric_survives():
    assert worktree.slugify("") == ""
    assert worktree.slugify("!!!") == ""
    # CJK is not in [a-z0-9], so a pure-CJK title derives NO slug: `create`
    # reports `bad-name` and the caller passes --name (the desktop dialog does).
    assert worktree.slugify("中文标题") == ""


# --- guards ----------------------------------------------------------------


def test_create_rejects_a_name_that_slugifies_to_nothing(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(worktree.WorktreeError) as err:
        worktree.create(str(repo), name="中文")
    assert err.value.code == "bad-name"
    assert err.value.message
    assert err.value.details == {}


def test_outside_a_git_repo_every_entry_point_raises_not_a_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    for call in (
        lambda: worktree.create(str(plain), name="me"),
        lambda: worktree.list_worktrees(str(plain)),
        lambda: worktree.remove(str(plain), "me"),
    ):
        with pytest.raises(worktree.WorktreeError) as err:
            call()
        assert err.value.code == "not-a-repo"


def test_create_from_the_worktree_container_is_refused(tmp_path):
    repo = _repo(tmp_path)
    container = repo / ".worktrees"
    container.mkdir()
    with pytest.raises(worktree.WorktreeError) as err:
        worktree.create(str(container), name="me")
    assert err.value.code == "in-worktree-container"
    assert not (container / "me").exists()


def test_create_with_a_missing_base_ref_raises_base_missing(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(worktree.WorktreeError) as err:
        worktree.create(str(repo), name="me", base="no-such-ref")
    assert err.value.code == "base-missing"
    assert not (repo / ".worktrees" / "me").exists()


def test_create_with_an_existing_explicit_branch_raises_branch_exists(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(worktree.WorktreeError) as err:
        worktree.create(str(repo), name="me", branch="master")
    assert err.value.code == "branch-exists"
    assert err.value.details["branch"] == "master"


# --- create ----------------------------------------------------------------


def test_create_makes_a_worktree_under_the_container_on_a_cluxmate_branch(tmp_path):
    repo = _repo(tmp_path)
    out = worktree.create(str(repo), name="My First Tree")
    assert out["ok"] is True
    assert out["name"] == "my-first-tree"
    assert out["branch"] == "cluxmate/my-first-tree"
    assert _same(out["path"], repo / ".worktrees" / "my-first-tree")
    assert Path(out["path"]).is_dir()
    assert _same(Path(out["path"]).parent, repo / ".worktrees")
    assert _same(out["project_root"], repo)
    assert _same(out["repo_root"], repo)
    assert len(out["base"]) == 40
    assert out["base_ref"] == "master"
    assert out["dirty"] == []
    assert _git_out("rev-parse", "--abbrev-ref", "HEAD", cwd=out["path"]).strip() == (
        "cluxmate/my-first-tree"
    )


def test_create_derives_the_slug_from_the_title_when_no_name_is_given(tmp_path):
    repo = _repo(tmp_path)
    out = worktree.create(str(repo), title="Fix the login bug")
    assert out["name"] == "fix-the-login-bug"
    assert out["branch"] == "cluxmate/fix-the-login-bug"


def test_create_honours_an_explicit_branch(tmp_path):
    repo = _repo(tmp_path)
    out = worktree.create(str(repo), name="me", branch="feature/custom")
    assert out["branch"] == "feature/custom"
    assert _same(out["path"], repo / ".worktrees" / "me")
    assert _git_out("rev-parse", "--abbrev-ref", "HEAD", cwd=out["path"]).strip() == (
        "feature/custom"
    )


def test_create_writes_worktrees_into_info_exclude_exactly_once(tmp_path):
    repo = _repo(tmp_path)
    worktree.create(str(repo), name="one")
    exclude = repo / ".git" / "info" / "exclude"
    assert ".worktrees/" in exclude.read_text(encoding="utf-8")
    # The point of the exclude: a new tree must not show up as untracked in the
    # main tree (it would otherwise be swallowed by `git add -A`).
    assert _git_out("status", "--porcelain", cwd=repo).strip() == ""
    worktree.create(str(repo), name="two")
    assert exclude.read_text(encoding="utf-8").count(".worktrees/") == 1


def test_create_does_not_touch_info_exclude_when_the_repo_already_ignores_it(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "ignore worktrees", cwd=repo)
    exclude = repo / ".git" / "info" / "exclude"
    before = exclude.read_text(encoding="utf-8")
    worktree.create(str(repo), name="one")
    assert exclude.read_text(encoding="utf-8") == before


def test_create_suffixes_a_slug_that_already_exists(tmp_path):
    repo = _repo(tmp_path)
    first = worktree.create(str(repo), name="me")
    second = worktree.create(str(repo), name="me")
    assert first["name"] == "me"
    assert second["name"] == "me-2"
    assert second["branch"] == "cluxmate/me-2"
    assert not _same(first["path"], second["path"])
    names = {row["name"] for row in worktree.list_worktrees(str(repo))}
    assert {"me", "me-2"} <= names


def test_create_refuses_a_dirty_main_tree_and_lists_the_files(tmp_path):
    repo = _repo(tmp_path)
    (repo / "wip.txt").write_text("wip\n", encoding="utf-8")
    with pytest.raises(worktree.WorktreeError) as err:
        worktree.create(str(repo), name="me")
    assert err.value.code == "dirty"
    assert "wip.txt" in err.value.details["files"]
    assert not (repo / ".worktrees" / "me").exists()


def test_create_allows_a_dirty_main_tree_with_allow_dirty(tmp_path):
    repo = _repo(tmp_path)
    (repo / "wip.txt").write_text("wip\n", encoding="utf-8")
    out = worktree.create(str(repo), name="me", allow_dirty=True)
    assert out["ok"] is True
    assert "wip.txt" in out["dirty"]
    assert Path(out["path"]).is_dir()


def test_create_inside_a_linked_worktree_lands_in_the_main_repo_container(tmp_path):
    repo = _repo(tmp_path)
    first = worktree.create(str(repo), name="first")
    second = worktree.create(first["path"], name="second")
    assert _same(second["repo_root"], repo)
    assert _same(second["project_root"], repo)
    assert _same(second["path"], repo / ".worktrees" / "second")
    assert _same(first["path"], repo / ".worktrees" / "first")


# --- info / list -----------------------------------------------------------


def test_info_wraps_project_root_resolution(tmp_path):
    repo = _repo(tmp_path)
    out = worktree.info(str(repo))
    assert out["ok"] is True
    assert out["is_worktree"] is False
    assert out["branch"] == "master"
    assert _same(out["cwd"], repo)
    assert _same(out["root"], repo)
    assert _same(out["config_root"], repo)


def test_info_reports_a_worktree_session_config_root_as_the_main_repo(tmp_path):
    repo = _repo(tmp_path)
    created = worktree.create(str(repo), name="me")
    out = worktree.info(created["path"])
    assert out["ok"] is True
    assert out["is_worktree"] is True
    assert out["branch"] == "cluxmate/me"
    assert _same(out["cwd"], created["path"])
    assert _same(out["root"], repo)
    assert _same(out["config_root"], repo)


def test_list_worktrees_names_entries_and_flags_main_and_current(tmp_path):
    repo = _repo(tmp_path)
    created = worktree.create(str(repo), name="me")
    rows = {_key(row["path"]): row for row in worktree.list_worktrees(str(repo))}
    main = rows[_key(repo)]
    assert main["name"] == ""
    assert main["branch"] == "master"
    assert len(main["head"]) == 40
    assert main["is_main"] is True
    mine = rows[_key(created["path"])]
    assert mine["name"] == "me"
    assert mine["branch"] == "cluxmate/me"
    assert len(mine["head"]) == 40
    assert mine["is_main"] is False
    assert mine["is_current"] is False
    # `is_current` is the resolved session cwd, not the repo root.
    inside = worktree.list_worktrees(created["path"])
    assert {row["is_current"] for row in inside} == {True, False}
    current = next(row for row in inside if row["is_current"])
    assert _same(current["path"], created["path"])


# --- remove ----------------------------------------------------------------


def test_remove_deletes_the_tree_and_its_branch(tmp_path):
    repo = _repo(tmp_path)
    created = worktree.create(str(repo), name="me")
    out = worktree.remove(str(repo), "me")
    assert out["ok"] is True
    assert out["pruned"] is False
    assert out["branch"] == "cluxmate/me"
    assert out["branch_deleted"] is True
    assert not Path(created["path"]).exists()
    assert _git_out("branch", "--list", "cluxmate/me", cwd=repo).strip() == ""
    assert all(
        not _same(row["path"], created["path"])
        for row in worktree.list_worktrees(str(repo))
    )


def test_remove_keep_branch_drops_the_tree_but_keeps_the_branch(tmp_path):
    repo = _repo(tmp_path)
    created = worktree.create(str(repo), name="me")
    out = worktree.remove(str(repo), "me", keep_branch=True)
    assert out["ok"] is True
    assert out["branch_deleted"] is False
    assert not Path(created["path"]).exists()
    assert "cluxmate/me" in _git_out("branch", "--list", "cluxmate/me", cwd=repo)


def test_remove_reports_a_branch_kept_when_it_is_checked_out_elsewhere(tmp_path):
    """`git branch -D` refusing is NOT a failure: the tree is gone, exit is 0."""
    repo = _repo(tmp_path)
    worktree.create(str(repo), name="me")
    # A second, hand-made tree takes the same branch, so `branch -D` must refuse.
    _git("worktree", "add", "--force", str(repo / ".worktrees" / "second"),
         "cluxmate/me", cwd=repo)
    out = worktree.remove(str(repo), "me")
    assert out["ok"] is True
    assert out["branch_deleted"] is False
    assert out["message"]
    assert "cluxmate/me" in _git_out("branch", "--list", "cluxmate/me", cwd=repo)


def test_remove_accepts_a_path_target(tmp_path):
    repo = _repo(tmp_path)
    created = worktree.create(str(repo), name="me")
    out = worktree.remove(str(repo), created["path"])
    assert out["ok"] is True
    assert not Path(created["path"]).exists()


def test_remove_refuses_a_dirty_worktree_without_force(tmp_path):
    repo = _repo(tmp_path)
    created = worktree.create(str(repo), name="me")
    (Path(created["path"]) / "wip.txt").write_text("wip\n", encoding="utf-8")
    with pytest.raises(worktree.WorktreeError) as err:
        worktree.remove(str(repo), "me")
    assert err.value.code == "dirty"
    assert "wip.txt" in err.value.details["files"]
    assert Path(created["path"]).is_dir()


def test_remove_force_discards_a_dirty_worktree(tmp_path):
    repo = _repo(tmp_path)
    created = worktree.create(str(repo), name="me")
    (Path(created["path"]) / "wip.txt").write_text("wip\n", encoding="utf-8")
    out = worktree.remove(str(repo), "me", force=True)
    assert out["ok"] is True
    assert not Path(created["path"]).exists()


def test_remove_the_main_worktree_is_refused(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(worktree.WorktreeError) as err:
        worktree.remove(str(repo), str(repo))
    assert err.value.code == "main-worktree"


def test_remove_an_unknown_name_raises_not_found(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(worktree.WorktreeError) as err:
        worktree.remove(str(repo), "nope")
    assert err.value.code == "not-found"


def test_remove_a_path_outside_the_container_raises_not_found(tmp_path):
    repo = _repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(worktree.WorktreeError) as err:
        worktree.remove(str(repo), str(outside))
    assert err.value.code == "not-found"


def test_remove_prunes_a_worktree_whose_directory_was_deleted(tmp_path):
    repo = _repo(tmp_path)
    created = worktree.create(str(repo), name="gone")
    shutil.rmtree(created["path"])
    out = worktree.remove(str(repo), "gone")
    assert out["ok"] is True
    assert out["pruned"] is True
    assert all(
        not _same(row["path"], created["path"])
        for row in worktree.list_worktrees(str(repo))
    )
    assert _git_out("branch", "--list", "cluxmate/gone", cwd=repo).strip() == ""


# --- CLI contract ----------------------------------------------------------


def test_cli_worktree_create_prints_exactly_one_json_line(tmp_path):
    repo = _repo(tmp_path)
    proc = _cli(["create", "--name", "me"], repo)
    assert proc.returncode == 0, proc.stderr
    payload = _one_json_line(proc)
    assert payload["ok"] is True
    assert payload["name"] == "me"
    assert payload["branch"] == "cluxmate/me"
    assert Path(payload["path"]).is_dir()


def test_cli_worktree_failure_prints_json_on_stdout_and_exits_one(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    proc = _cli(["create", "--name", "me"], plain)
    assert proc.returncode == 1
    payload = _one_json_line(proc)
    assert payload["ok"] is False
    assert payload["error"] == "not-a-repo"
    assert payload["message"]
    assert payload["details"] == {}


def test_cli_worktree_remove_after_a_manual_delete_exits_zero(tmp_path):
    repo = _repo(tmp_path)
    created = worktree.create(str(repo), name="gone")
    shutil.rmtree(created["path"])
    proc = _cli(["remove", "gone"], repo)
    assert proc.returncode == 0, proc.stderr
    payload = _one_json_line(proc)
    assert payload["ok"] is True
    assert payload["pruned"] is True
