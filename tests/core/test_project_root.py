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


def test_subdir_of_a_plain_repo_is_not_a_worktree(tmp_path):
    """普通仓库的子目录不是 linked worktree：git 的 dir 就是 common dir。"""
    repo = _repo(tmp_path)
    sub = repo / "pkg"
    sub.mkdir()
    info = project_root.resolve(str(sub))
    assert info.root == str(repo.resolve())
    assert info.is_worktree is False
    assert info.branch == "main"


def test_linked_worktree_resolves_to_the_main_root(tmp_path):
    repo = _repo(tmp_path)
    wt = repo / "wt"
    _git("worktree", "add", "-b", "wt/x", str(wt), cwd=repo)
    info = project_root.resolve(str(wt))
    assert info.root == str(repo.resolve())
    assert info.is_worktree is True
    assert info.branch == "wt/x"


def test_subdir_inside_a_linked_worktree_is_a_worktree(tmp_path):
    """linked worktree 内部的子目录同样是 worktree，root 仍是主仓库根。"""
    repo = _repo(tmp_path)
    wt = repo / "wt"
    _git("worktree", "add", "-b", "wt/z", str(wt), cwd=repo)
    sub = wt / "pkg"
    sub.mkdir()
    info = project_root.resolve(str(sub))
    assert info.root == str(repo.resolve())
    assert info.is_worktree is True
    assert info.branch == "wt/z"


def test_bare_repo_passed_as_cwd_is_its_own_root(tmp_path):
    """common dir 就是 cwd 的裸仓库：自己是自己的 root，且不是 worktree。"""
    bare = tmp_path / "bare.git"
    bare.mkdir()
    _git("init", "--bare", cwd=bare)
    info = project_root.resolve(str(bare))
    assert info.root == str(bare.resolve())
    assert info.is_worktree is False


def test_git_missing_degrades_to_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    info = project_root.resolve(str(tmp_path))
    assert info.root == str(tmp_path.resolve())
    assert info.is_worktree is False


def test_unstatable_cwd_degrades_to_cwd(tmp_path):
    """Embedded NUL: `Path.resolve` raises ValueError *and* git cannot run there.

    Both halves are needed for "never raises" — catching ValueError around the
    key alone would just move the crash into `_probe` (subprocess rejects a NUL
    argument with the same ValueError).
    """
    bad = str(tmp_path / "repo") + "\x00sub"
    info = project_root.resolve(bad)
    assert info.cwd == bad
    assert info.root == bad
    assert info.is_worktree is False
    assert info.config_root == bad


def test_relative_git_common_dir_fallback(tmp_path, monkeypatch):
    """`--path-format=absolute` 不可用（git < 2.31）时走第二种调用形式。"""
    repo = _repo(tmp_path)
    wt = repo / "wt"
    _git("worktree", "add", "-b", "wt/y", str(wt), cwd=repo)
    monkeypatch.setattr(project_root, "_COMMON_DIR_ARG_SETS",
                        (("--git-common-dir",),))
    info = project_root.resolve(str(wt))
    assert info.root == str(repo.resolve())
    # 相对形式会回来 `<wt>/../.git` 一类的路径，root 必须是折叠干净的绝对路径。
    assert ".." not in info.root


def test_relative_fallback_normalises_a_dotdot_root(tmp_path, monkeypatch):
    """回退形式从仓库子目录回来的是 `<repo>/pkg/../.git`，root 必须折叠成 `<repo>`。"""
    repo = _repo(tmp_path)
    sub = repo / "pkg"
    sub.mkdir()
    monkeypatch.setattr(project_root, "_COMMON_DIR_ARG_SETS",
                        (("--git-common-dir",),))
    monkeypatch.setattr(project_root, "_GIT_DIR_ARG_SETS", (("--git-dir",),))
    info = project_root.resolve(str(sub))
    assert info.root == str(repo.resolve())
    assert ".." not in info.root
    assert info.is_worktree is False


def test_config_root_of_a_non_git_dir_is_the_cwd(tmp_path):
    """配置根策略：非 git 目录什么都不改写。"""
    info = project_root.resolve(str(tmp_path))
    assert info.config_root == str(tmp_path.resolve())


def test_config_root_of_a_plain_repo_root_is_itself(tmp_path):
    repo = _repo(tmp_path)
    info = project_root.resolve(str(repo))
    assert info.config_root == str(repo.resolve())


def test_config_root_of_a_plain_repo_subdir_is_the_subdir(tmp_path):
    """普通仓库的子目录不是 worktree：配置根留在子目录自己身上。

    `root`（主工作树根）仍然是仓库根，但那是信息字段；读配置的调用方走
    `config_root`，所以 `<repo>/pkg` 的会话读 `<repo>/pkg/.cluxmate`——与本
    plan 之前逐字节一致。
    """
    repo = _repo(tmp_path)
    sub = repo / "pkg"
    sub.mkdir()
    info = project_root.resolve(str(sub))
    assert info.root == str(repo.resolve())
    assert info.is_worktree is False
    assert info.config_root == str(sub.resolve())


def test_config_root_of_a_linked_worktree_is_the_main_root(tmp_path):
    repo = _repo(tmp_path)
    wt = repo / "wt"
    _git("worktree", "add", "-b", "wt/w", str(wt), cwd=repo)
    info = project_root.resolve(str(wt))
    assert info.config_root == str(repo.resolve())


def test_config_root_of_a_subdir_inside_a_linked_worktree_is_the_main_root(tmp_path):
    repo = _repo(tmp_path)
    wt = repo / "wt"
    _git("worktree", "add", "-b", "wt/s", str(wt), cwd=repo)
    sub = wt / "pkg"
    sub.mkdir()
    info = project_root.resolve(str(sub))
    assert info.root == str(repo.resolve())
    assert info.is_worktree is True
    assert info.config_root == str(repo.resolve())


def test_config_root_degrades_to_cwd_without_git(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    info = project_root.resolve(str(tmp_path))
    assert info.config_root == str(tmp_path.resolve())


def test_result_is_cached_until_cleared(tmp_path):
    repo = _repo(tmp_path)
    first = project_root.resolve(str(repo))
    assert project_root.resolve(str(repo)) is first
    project_root.clear_cache()
    assert project_root.resolve(str(repo)) is not first
