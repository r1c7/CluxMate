"""项目根解析：主工作树、回退链、缓存。"""

import shutil
import subprocess
from pathlib import Path

import pytest

from cluxmate.core import project_root

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _git(*args, cwd=None):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "-c", "init.defaultBranch=main", *args],
        cwd=cwd, check=True, capture_output=True,
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", cwd=repo)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "init", cwd=repo)
    return repo


@pytest.fixture(autouse=True)
def _no_cache():
    project_root.clear_cache()
    yield
    project_root.clear_cache()


def test_non_git_dir_is_its_own_root(tmp_path):
    info = project_root.resolve(str(tmp_path))
    assert info.root == str(tmp_path.resolve())
    assert info.is_worktree is False
    assert info.branch is None


def test_plain_repo_is_its_own_root(tmp_path):
    repo = _repo(tmp_path)
    info = project_root.resolve(str(repo))
    assert info.root == str(repo.resolve())
    assert info.is_worktree is False
    assert info.branch == "main"


def test_subdir_of_a_repo_resolves_to_the_repo_root(tmp_path):
    repo = _repo(tmp_path)
    sub = repo / "pkg"
    sub.mkdir()
    assert project_root.resolve(str(sub)).root == str(repo.resolve())


def test_linked_worktree_resolves_to_the_main_root(tmp_path):
    repo = _repo(tmp_path)
    wt = repo / "wt"
    _git("worktree", "add", "-b", "wt/x", str(wt), cwd=repo)
    info = project_root.resolve(str(wt))
    assert info.root == str(repo.resolve())
    assert info.is_worktree is True
    assert info.branch == "wt/x"


def test_git_missing_degrades_to_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    info = project_root.resolve(str(tmp_path))
    assert info.root == str(tmp_path.resolve())
    assert info.is_worktree is False


def test_relative_git_common_dir_fallback(tmp_path, monkeypatch):
    """`--path-format=absolute` 不可用（git < 2.31）时走第二种调用形式。"""
    repo = _repo(tmp_path)
    wt = repo / "wt"
    _git("worktree", "add", "-b", "wt/y", str(wt), cwd=repo)
    monkeypatch.setattr(project_root, "_COMMON_DIR_ARG_SETS",
                        (("--git-common-dir",),))
    assert project_root.resolve(str(wt)).root == str(repo.resolve())


def test_result_is_cached_until_cleared(tmp_path):
    repo = _repo(tmp_path)
    first = project_root.resolve(str(repo))
    assert project_root.resolve(str(repo)) is first
    project_root.clear_cache()
    assert project_root.resolve(str(repo)) is not first
