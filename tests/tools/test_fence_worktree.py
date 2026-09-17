"""WriteFence 在 worktree 会话下的 roots/deny 语义。

注意：测试的 tmp_path 落在系统 temp 里，而系统 temp 本身是一个可写根，
所以每个用例都把 `tempfile.gettempdir` 换成一个无关目录——否则"放行"可能
是因为 temp 根，而不是我们想验的那条规则。
"""

import tempfile
from pathlib import Path

import pytest

from cluxmate.tools._fence import SandboxViolation, WriteFence


@pytest.fixture
def layout(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "no-temp"))
    repo = tmp_path / "repo"
    me = repo / ".worktrees" / "me"
    sibling = repo / ".worktrees" / "other"
    for d in (me / "src", sibling / "src", repo / ".cluxmate"):
        d.mkdir(parents=True, exist_ok=True)
    return repo, me, sibling


def test_worktree_session_writes_its_own_tree(layout):
    repo, me, _ = layout
    fence = WriteFence(str(me), project_root=str(repo))
    assert fence.check(me / "src" / "f.py") == (me / "src" / "f.py").resolve()


def test_worktree_session_cannot_write_a_sibling_worktree(layout):
    repo, me, sibling = layout
    fence = WriteFence(str(me), project_root=str(repo))
    with pytest.raises(SandboxViolation):
        fence.check(sibling / "src" / "f.py")
    with pytest.raises(SandboxViolation):
        fence.check(sibling / "src" / "f.py", escalate=True)


def test_worktree_session_cannot_write_its_own_state_dir(layout):
    repo, me, _ = layout
    fence = WriteFence(str(me), project_root=str(repo))
    with pytest.raises(SandboxViolation):
        fence.check(me / ".cluxmate" / "permissions.json", escalate=True)


def test_worktree_session_cannot_write_the_main_repo_state(layout):
    repo, me, _ = layout
    fence = WriteFence(str(me), project_root=str(repo))
    with pytest.raises(SandboxViolation):
        fence.check(repo / ".cluxmate" / "permissions.json")
    with pytest.raises(SandboxViolation):
        fence.check(repo / ".cluxmate" / "permissions.json", escalate=True)


def test_worktree_session_cannot_write_the_main_tree(layout):
    repo, me, _ = layout
    fence = WriteFence(str(me), project_root=str(repo))
    with pytest.raises(SandboxViolation):
        fence.check(repo / "src" / "f.py")


def test_worktree_session_may_write_the_project_memory_file(layout):
    repo, me, _ = layout
    fence = WriteFence(str(me), project_root=str(repo))
    assert fence.check(repo / "AGENTS.md") == (repo / "AGENTS.md").resolve()


def test_main_session_cannot_write_any_worktree(layout):
    repo, me, sibling = layout
    fence = WriteFence(str(repo), project_root=str(repo))
    for target in (me / "src" / "f.py", sibling / "src" / "f.py"):
        with pytest.raises(SandboxViolation):
            fence.check(target)
        with pytest.raises(SandboxViolation):
            fence.check(target, escalate=True)


def test_denyroots_of_a_main_session_are_deduped(layout):
    repo, _, _ = layout
    roots = WriteFence(str(repo), project_root=str(repo)).denyroots()
    assert roots.count((repo / ".cluxmate").resolve()) == 1
    assert (repo / ".worktrees").resolve() in roots


def test_without_project_root_behaviour_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "no-temp"))
    ws = tmp_path / "ws"
    ws.mkdir()
    fence = WriteFence(str(ws))
    assert fence.check(ws / "f.py") == (ws / "f.py").resolve()
    with pytest.raises(SandboxViolation):
        fence.check(ws / ".cluxmate" / "permissions.json")
