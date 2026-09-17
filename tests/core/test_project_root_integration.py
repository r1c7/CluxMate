"""一个真 git worktree 里的会话必须看到主仓的项目配置。

和前几个任务不同，这里不造假布局：仓库和 worktree 都是真的 git 产物，所以它
同时钉住 `project_root.resolve` 的解析结果与三个入口（builder / CLI / TUI）的接线。
"""

import argparse
import asyncio
import json
import os
import subprocess
from pathlib import Path

from cluxmate import cli
from cluxmate.core.builder import AgentBuilder
from cluxmate.core.project_root import resolve
from cluxmate.core.trust import TRUSTED, canonical
from cluxmate.tui import controller as controller_mod
from cluxmate.tui.controller import TuiController


class _Provider:
    def set_reasoning_effort(self, effort):
        pass


def _git(*args, cwd=None):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd, check=True, capture_output=True,
    )


def _repo_with_worktree(tmp_path) -> tuple[Path, Path]:
    """A real repo shipping project config + a real linked worktree of it."""
    repo = tmp_path / "repo"
    (repo / ".cluxmate" / "skills" / "demo").mkdir(parents=True)
    (repo / ".cluxmate" / "skills" / "demo" / "SKILL.md").write_text(
        "---\nname: Demo\ndescription: demo skill\n---\nbody\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("# main repo memory\n", encoding="utf-8")
    _git("init", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "init", cwd=repo)
    wt = repo / ".worktrees" / "me"
    _git("worktree", "add", "-b", "wt/me", str(wt), cwd=repo)
    return repo, wt


def _home(tmp_path, monkeypatch) -> Path:
    """Redirect the global config root so a real ~/.cluxmate cannot leak in."""
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(cli, "_TRUST_STORE", None)
    return home


def test_a_worktree_session_sees_the_main_repo_skills_and_memory(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    repo, wt = _repo_with_worktree(tmp_path)

    info = resolve(str(wt))
    b = AgentBuilder(str(wt), _Provider(), project_root=info.config_root)
    parts = dict(b.render_injections())
    assert "main repo memory" in parts["memory"]
    assert "demo" in parts["skill"]
    # 树仍然是会话自己的树
    assert b.cwd == str(wt)
    assert info.is_worktree is True
    # 也只有链接工作树才重定向：主仓自己仍是自己的配置根
    assert Path(info.config_root) == repo
    assert Path(resolve(str(repo)).config_root) == repo


def test_a_worktree_session_reads_config_the_main_repo_keeps_untracked(tmp_path, monkeypatch):
    """上面那条是回归锁（配置已提交 ⇒ 工作树检出里也有一份，接不接线都过）。

    真正能分辨两条根的，只有"主仓有、工作树没有"的配置态：主仓里未提交的
    skill 必须能被工作树会话读到，而没拿到 project_root 的 builder
    （= cwd 自己）读不到——这正是 Task 8 接线的意义。
    """
    _home(tmp_path, monkeypatch)
    repo, wt = _repo_with_worktree(tmp_path)
    later = repo / ".cluxmate" / "skills" / "untracked"
    later.mkdir()
    (later / "SKILL.md").write_text(
        "---\nname: Untracked\ndescription: added after the commit\n---\nbody\n",
        encoding="utf-8",
    )
    assert not (wt / ".cluxmate" / "skills" / "untracked").exists()  # 未提交 ⇒ 工作树里没有

    wired = dict(AgentBuilder(
        str(wt), _Provider(), project_root=resolve(str(wt)).config_root,
    ).render_injections())
    unwired = dict(AgentBuilder(str(wt), _Provider()).render_injections())

    assert "untracked" in wired["skill"]
    assert "untracked" not in unwired["skill"]


# ── 入口一：CLI（headless / REPL 共用同一条构造路径） ───────────────────────


class _FakeHooks:
    def has_event(self, name):
        return False


class _FakeResult:
    text = "ok"


class _FakeAgent:
    async def run(self, prompt, callbacks=None, injections=None):
        return _FakeResult()


class _FakeBuilder:
    """Records what `run_headless` hands the real AgentBuilder."""

    built: list["_FakeBuilder"] = []

    def __init__(self, cwd, provider, project_root=None):
        self.cwd = cwd
        self.project_root = project_root
        self.trust = None
        _FakeBuilder.built.append(self)

    def with_default_tools(self):
        return self

    def with_subagents(self):
        return self

    def with_model(self, name):
        return self

    def with_context_1m(self, flag):
        return self

    def with_trust(self, decision):
        self.trust = decision
        return self

    def _hooks_manager(self):
        return _FakeHooks()

    def injections_for_turn(self):
        return []

    def build(self, session_log=None):
        return _FakeAgent()


def test_the_cli_hands_the_builder_the_project_root_not_the_session_tree(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    repo, wt = _repo_with_worktree(tmp_path)
    monkeypatch.chdir(wt)  # a session started inside the worktree
    import cluxmate.core.providers.factory as factory

    monkeypatch.setattr(factory, "build_provider", lambda entry: _Provider())
    monkeypatch.setattr(
        cli, "_resolve_entry",
        lambda model_id=None: {"provider": "p", "model_name": "m"},
    )
    monkeypatch.setattr(cli, "AgentBuilder", _FakeBuilder)
    cli._trust_store().set(str(repo), TRUSTED)
    _FakeBuilder.built = []

    asyncio.run(cli.run_headless("hi"))

    b = _FakeBuilder.built[-1]
    assert Path(b.project_root) == repo  # config readers take the project root
    assert Path(b.cwd) == wt  # the writable tree stays the session's
    assert b.trust.trusted is True  # the trusted main repo is inherited


def test_the_mcp_command_reads_the_projects_config_from_a_worktree(tmp_path, monkeypatch, capsys):
    """`cluxmate mcp --cwd <worktree>` 读的是项目的 mcp.json，不是工作树自己的。

    mcp.json 故意不提交：工作树检出里没有它，所以"看到了 proj-server"只可能
    来自 config root 的重定向。
    """
    _home(tmp_path, monkeypatch)
    repo, wt = _repo_with_worktree(tmp_path)
    (repo / ".cluxmate" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"proj-server": {"url": "https://repo.example/mcp"}}}),
        encoding="utf-8",
    )
    assert not (wt / ".cluxmate" / "mcp.json").exists()
    cli._trust_store().set(str(repo), TRUSTED)

    args = argparse.Namespace(mcp_command="status", name=None, cwd=str(wt), json=False)
    assert cli.run_mcp(args) == 0
    assert "proj-server" in capsys.readouterr().out


def test_trust_add_from_a_worktree_records_the_project(tmp_path, monkeypatch):
    """`trust add` inside a worktree must file the answer where sessions look."""
    home = _home(tmp_path, monkeypatch)
    repo, wt = _repo_with_worktree(tmp_path)
    monkeypatch.chdir(wt)

    assert cli.run_trust(argparse.Namespace(action="add", path=str(wt))) == 0

    folders = json.loads(
        (home / ".cluxmate" / "trust.json").read_text("utf-8")
    )["folders"]
    # Exactly one entry, the project's: the worktree spelling is not a project.
    assert [os.path.normcase(k) for k in folders] == [
        os.path.normcase(canonical(str(repo)))
    ]
    assert cli._trust_decision(str(wt)).trusted is True
    assert cli._trust_decision(str(repo)).trusted is True


# ── 入口二：TUI 控制器 ─────────────────────────────────────────────────────


class _StubBuilder:
    """Records the constructor arguments `_build_agent` hands the real builder."""

    built: list["_StubBuilder"] = []

    def __init__(self, cwd, llm_provider, project_root=None):
        self.cwd = cwd
        self.project_root = project_root
        _StubBuilder.built.append(self)

    def with_trust(self, decision):
        return self

    def with_default_tools(self):
        return self

    def with_subagents(self):
        return self

    def with_mode(self, mode):
        return self

    def with_model(self, model_name):
        return self

    def with_context_1m(self, flag):
        return self

    def with_mcp(self, mcp):
        self.mcp = mcp
        return self

    def with_log_store(self, store):
        return self

    def build(self, session_log=None):
        return object()


class _StubMCP:
    """Records (exec tree, config root); never spawns a server."""

    def __init__(self, cwd, *, trusted=True, config_root=None):
        self.cwd = cwd
        self.config_root = config_root
        self._trusted = trusted

    def load(self):
        return {}


class _StubProvider:
    def set_reasoning_effort(self, effort):
        pass


def test_the_tui_keys_the_builder_policy_mcp_and_trust_on_the_project_root(
    tmp_path, monkeypatch
):
    _home(tmp_path, monkeypatch)
    repo, wt = _repo_with_worktree(tmp_path)
    _StubBuilder.built = []
    monkeypatch.setattr(controller_mod, "AgentBuilder", _StubBuilder)
    monkeypatch.setattr(controller_mod, "MCPManager", _StubMCP)
    monkeypatch.setattr(controller_mod, "_create_provider", lambda entry: _StubProvider())
    entry = {
        "id": "m1", "api_key": "k", "model_name": "the-model",
        "provider": "openai", "api_type": "openai",
    }
    ctrl = TuiController()
    monkeypatch.setattr(ctrl.config, "get_model", lambda model_id: entry)
    ctrl.set_trust(str(repo), TRUSTED)  # the project was trusted in an earlier run
    ctrl.new_session("m1", str(wt), "default")

    b = _StubBuilder.built[-1]
    assert Path(b.cwd) == wt
    assert Path(b.project_root) == repo
    # Always-allow rules are the PROJECT's...
    assert Path(ctrl._policy._store._path) == repo / ".cluxmate" / "permissions.json"
    # ...and so is mcp.json, while the servers still run in the session's tree.
    mcp = ctrl._ensure_mcp(str(wt))
    assert Path(mcp.cwd) == wt
    assert Path(mcp.config_root) == repo
    # The worktree inherits the answer without ever growing a trust entry of its own.
    assert ctrl.trust_for(str(wt)).trusted is True
    assert [os.path.normcase(k) for k in ctrl._trust_store.entries()] == [
        os.path.normcase(canonical(str(repo)))
    ]
    assert ctrl.trust_for(str(wt)).source == "registry"
