"""initialize 解析一次项目根：trust/policy/hooks 走主仓，checkpoint 走会话树。"""

import json
import subprocess
from pathlib import Path

from cluxmate.core import jsonrpc_server
from cluxmate.core.jsonrpc_server import JsonRpcServer
from cluxmate.core.session_log import SessionHeader, SessionLog


class _Provider:
    def set_reasoning_effort(self, effort):
        pass


def _git(*args, cwd=None):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd, check=True, capture_output=True,
    )


def _server(tmp_path, monkeypatch, cwd: Path) -> JsonRpcServer:
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    # initialize's result payload would otherwise land on the test's stdout.
    monkeypatch.setattr(jsonrpc_server, "_write_dict", lambda payload: None)
    monkeypatch.setattr(
        JsonRpcServer, "_build_provider",
        lambda self, model_id: (_Provider(), "test", False,
                                {"id": "test", "model_name": "test"}))
    monkeypatch.setattr(
        JsonRpcServer, "_load_or_create_log",
        lambda self, session_id, entry: (
            SessionLog.create(SessionHeader(id="s1", createdAt=0)), True))
    monkeypatch.setattr(JsonRpcServer, "_bind_persister", lambda self: None)
    s = JsonRpcServer()
    s._cwd = str(cwd)
    return s


def _repo_with_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", cwd=repo)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    (repo / ".cluxmate").mkdir(exist_ok=True)
    (repo / ".cluxmate" / "permissions.json").write_text(
        json.dumps({"always_allow_tools": ["todo_write"]}), encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "init", cwd=repo)
    wt = repo / ".worktrees" / "me"
    _git("worktree", "add", "-b", "wt/me", str(wt), cwd=repo)
    return repo, wt


def _repo_with_subdir(tmp_path):
    """普通仓库 + 子目录，两棵树上的 `.cluxmate/` 故意写得不一样。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", cwd=repo)
    (repo / ".cluxmate").mkdir(exist_ok=True)
    (repo / ".cluxmate" / "permissions.json").write_text(
        json.dumps({"always_allow_tools": ["todo_write"]}), encoding="utf-8")
    sub = repo / "pkg"
    sub.mkdir()
    (sub / ".cluxmate").mkdir(exist_ok=True)
    (sub / ".cluxmate" / "permissions.json").write_text(
        json.dumps({"always_allow_tools": ["read_file"]}), encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "init", cwd=repo)
    return repo, sub


def test_project_root_exists_before_initialize(tmp_path, monkeypatch):
    """未 initialize 的 server 也不能在 `_project_root` 上炸 AttributeError。"""
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    s = JsonRpcServer()
    assert s._project_root == s._cwd


def test_initialize_resolves_the_project_root(tmp_path, monkeypatch):
    repo, wt = _repo_with_worktree(tmp_path)
    s = _server(tmp_path, monkeypatch, wt)
    s._handle_initialize(1, {"session_id": "s1", "cwd": str(wt)})
    assert Path(s._cwd).resolve() == wt.resolve()
    assert Path(s._project_root).resolve() == repo.resolve()


def test_trust_is_recorded_and_resolved_on_the_project_root(tmp_path, monkeypatch):
    repo, wt = _repo_with_worktree(tmp_path)
    s = _server(tmp_path, monkeypatch, wt)
    s._handle_initialize(1, {"session_id": "s1", "cwd": str(wt),
                             "trust": "trusted"})
    # 会话级答案落在项目根上，worktree 路径本身不进注册表
    assert s._trust_store.session_status(str(repo)) == "trusted"


def test_checkpoints_stay_on_the_session_tree(tmp_path, monkeypatch):
    repo, wt = _repo_with_worktree(tmp_path)
    s = _server(tmp_path, monkeypatch, wt)
    s._handle_initialize(1, {"session_id": "s1", "cwd": str(wt)})
    assert Path(s._checkpoints._cwd) == wt.resolve()


def test_trust_rpcs_answer_about_the_project_root(tmp_path, monkeypatch):
    """trust/get|set 的目标目录是项目根：写在 worktree 上的答案必须读得回来。"""
    repo, wt = _repo_with_worktree(tmp_path)
    s = _server(tmp_path, monkeypatch, wt)
    s._handle_initialize(1, {"session_id": "s1", "cwd": str(wt)})
    result = s._trust_set({"cwd": str(wt), "status": "trusted"})
    assert result["status"] == "trusted"
    assert Path(result["cwd"]) == repo.resolve()
    keys = {Path(k).resolve() for k in s._trust_store.entries()}
    assert keys == {repo.resolve()}
    # 会话树自己的拼写不留注册表条目（否则同一个项目会有两条答案）
    assert Path(str(wt)).resolve() not in keys


def test_agents_list_reads_the_project_root_agents(tmp_path, monkeypatch):
    repo, wt = _repo_with_worktree(tmp_path)
    (repo / ".cluxmate" / "agents").mkdir(parents=True, exist_ok=True)
    (repo / ".cluxmate" / "agents" / "auditor.md").write_text(
        "---\nname: auditor\ndescription: audit\n---\nbody\n", encoding="utf-8")
    s = _server(tmp_path, monkeypatch, wt)
    s._handle_initialize(1, {"session_id": "s1", "cwd": str(wt)})
    s._trust_store.set(str(repo), "trusted")
    slugs = [e["slug"] for e in s._agents_snapshot({"cwd": str(wt)})["agents"]]
    assert "auditor" in slugs


def test_policy_reads_the_project_permissions(tmp_path, monkeypatch):
    """会话树里那份 permissions.json 不是策略来源（两棵树故意写得不一样）。"""
    repo, wt = _repo_with_worktree(tmp_path)
    (wt / ".cluxmate" / "permissions.json").write_text(
        json.dumps({"always_allow_tools": ["read_file"]}), encoding="utf-8")
    s = _server(tmp_path, monkeypatch, wt)
    s._handle_initialize(1, {"session_id": "s1", "cwd": str(wt),
                             "trust": "trusted"})
    assert s._policy.snapshot()["always_allow_tools"] == ["todo_write"]


def test_hooks_split_exec_tree_from_config_root(tmp_path, monkeypatch):
    """hooks 在两棵树上分别取值：配置来自主仓，执行树是会话树。"""
    repo, wt = _repo_with_worktree(tmp_path)
    s = _server(tmp_path, monkeypatch, wt)
    s._handle_initialize(1, {"session_id": "s1", "cwd": str(wt)})
    hooks = s._builder._hooks_manager()
    assert Path(hooks._cwd) == wt.resolve()
    assert Path(hooks._config_root) == repo.resolve()


# ── 回归锁：普通仓库的子目录会话不被改写成仓库根 ──────────────────


def test_plain_repo_subdir_session_keeps_its_own_config_root(tmp_path, monkeypatch):
    """`<repo>/pkg` 不是 worktree：配置根、信任、策略都留在 `<repo>/pkg`。"""
    repo, sub = _repo_with_subdir(tmp_path)
    s = _server(tmp_path, monkeypatch, sub)
    s._handle_initialize(1, {"session_id": "s1", "cwd": str(sub),
                             "trust": "trusted"})
    assert Path(s._project_root).resolve() == sub.resolve()
    # 会话级答案落在子目录上，仓库根上不留条目
    assert s._trust_store.session_status(str(sub)) == "trusted"
    assert s._trust_store.session_status(str(repo)) == "unknown"
    # 子目录自己的 permissions.json 才是策略来源
    assert s._policy.snapshot()["always_allow_tools"] == ["read_file"]


def test_plain_repo_subdir_trust_rpc_answers_about_the_subdir(tmp_path, monkeypatch):
    repo, sub = _repo_with_subdir(tmp_path)
    s = _server(tmp_path, monkeypatch, sub)
    s._handle_initialize(1, {"session_id": "s1", "cwd": str(sub)})
    result = s._trust_set({"cwd": str(sub), "status": "trusted"})
    assert Path(result["cwd"]) == sub.resolve()
    keys = {Path(k).resolve() for k in s._trust_store.entries()}
    assert keys == {sub.resolve()}


def test_plain_repo_subdir_agents_list_reads_the_subdir(tmp_path, monkeypatch):
    repo, sub = _repo_with_subdir(tmp_path)
    (sub / ".cluxmate" / "agents").mkdir(parents=True, exist_ok=True)
    (sub / ".cluxmate" / "agents" / "auditor.md").write_text(
        "---\nname: auditor\ndescription: audit\n---\nbody\n", encoding="utf-8")
    s = _server(tmp_path, monkeypatch, sub)
    s._handle_initialize(1, {"session_id": "s1", "cwd": str(sub)})
    s._trust_store.set(str(sub), "trusted")
    slugs = [e["slug"] for e in s._agents_snapshot({"cwd": str(sub)})["agents"]]
    assert "auditor" in slugs
