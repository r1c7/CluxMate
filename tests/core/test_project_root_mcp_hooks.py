"""MCP / hooks: config from the CONFIG ROOT, execution from the session tree.

Task 5 made the builder read project config from `project_root`. For MCP and
hooks that root is also an EXECUTION location, so it cannot simply be swapped
wholesale:

- ``MCPManager._cwd`` is (a) the spawn cwd of every stdio server and (b) the
  spill cwd behind ``MCPToolWrapper._workdir`` → ``<cwd>/.cluxmate/tmp-spill/``.
- ``HookManager._cwd`` is (a) the hook process's cwd and (b) the ``"cwd"``
  field of the JSON payload every hook receives on stdin.

A worktree session runs with ``cwd`` = the writable session tree
(``<repo>/.worktrees/me``) and ``config_root`` = the main repo: config must come
from the main repo while spawn / spill / payload stay on the session tree.
Every test here pins BOTH halves of that split, so reverting either half fails.
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from cluxmate.core import builder as builder_mod
from cluxmate.core.builder import AgentBuilder
from cluxmate.core.hooks import HookManager
from cluxmate.core.mcp import MCPConfigManager, MCPManager

_FAKE_SERVER = Path(__file__).parent / "fake_mcp_server.py"

# Prints its OWN process cwd plus the whole hook payload it got on stdin, both
# wrapped in hookSpecificOutput.additionalContext (the feedback channel).
_HELPER = '''import json, os, sys
data = json.load(sys.stdin)
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": data.get("hook_event_name"),
    "additionalContext": json.dumps({
        "payload": data,
        "process_cwd": os.getcwd(),
    }),
}}))
'''


def _home(monkeypatch, tmp_path) -> Path:
    """Redirect the global config root so a real ~/.cluxmate cannot leak in."""
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    return home


def _repo_and_worktree(tmp_path) -> tuple[Path, Path]:
    """A main repo and a session worktree inside it (both without config)."""
    repo = tmp_path / "repo"
    wt = repo / ".worktrees" / "me"
    (repo / ".cluxmate").mkdir(parents=True, exist_ok=True)
    wt.mkdir(parents=True, exist_ok=True)
    return repo, wt


def _write_mcp_json(root: Path, servers: dict) -> None:
    (root / ".cluxmate").mkdir(parents=True, exist_ok=True)
    (root / ".cluxmate" / "mcp.json").write_text(
        json.dumps({"mcpServers": servers}), encoding="utf-8"
    )


def _write_settings(root: Path, hooks: dict) -> None:
    (root / ".cluxmate").mkdir(parents=True, exist_ok=True)
    (root / ".cluxmate" / "settings.json").write_text(
        json.dumps({"hooks": hooks}), encoding="utf-8"
    )


def _stdio_server() -> dict:
    return {"command": sys.executable, "args": [str(_FAKE_SERVER)],
            "risk_level": "safe"}


def _hook_entry(script: Path) -> dict:
    return {"hooks": [{"type": "command",
                       "command": f'"{sys.executable}" "{script}"'}]}


class _RecordingSandbox:
    """ShellSandbox stand-in that records the cwd each spawn is given."""

    def __init__(self) -> None:
        self.spawn_cwds: list[str] = []

    def spawn_popen(self, cmd, cwd=None, env=None):
        self.spawn_cwds.append(cwd)
        return subprocess.Popen(
            cmd, cwd=cwd, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=1,
            text=True, encoding="utf-8", errors="replace",
        )


# ── MCP: config root vs exec tree ───────────────────────────────────────────

def test_mcp_servers_come_from_the_config_root_not_the_session_tree(
    tmp_path, monkeypatch
):
    """The main repo's mcp.json is read; the worktree's own one is not."""
    _home(monkeypatch, tmp_path)
    repo, wt = _repo_and_worktree(tmp_path)
    _write_mcp_json(repo, {"from-main": _stdio_server()})
    _write_mcp_json(wt, {"from-worktree": _stdio_server()})

    mgr = MCPManager(str(wt), trusted=True, config_root=str(repo))

    mgr.load()
    try:
        assert mgr.config("from-main") is not None
        assert mgr.config("from-worktree") is None
        names = {s["name"] for s in mgr.status()}
        assert names == {"from-main"}
        client = mgr.client("from-main")
        assert client is not None
        # ... and the client still executes in the session tree.
        assert Path(client._cwd).resolve() == wt.resolve()
        assert Path(client._cwd).resolve() != repo.resolve()
    finally:
        mgr.shutdown()


def test_stdio_servers_spawn_in_the_session_tree(tmp_path, monkeypatch):
    """The spawn cwd handed to the sandbox is the session tree, not the repo."""
    _home(monkeypatch, tmp_path)
    repo, wt = _repo_and_worktree(tmp_path)
    _write_mcp_json(repo, {"from-main": _stdio_server()})
    sandbox = _RecordingSandbox()

    mgr = MCPManager(str(wt), sandbox=sandbox, config_root=str(repo))
    mgr.load()
    try:
        assert [Path(c).resolve() for c in sandbox.spawn_cwds] == [wt.resolve()]
        assert mgr.client("from-main")._status == "connected"
    finally:
        mgr.shutdown()


def test_oversized_mcp_results_spill_under_the_session_tree(
    tmp_path, monkeypatch
):
    """The spill file lands in <session tree>/.cluxmate/tmp-spill/, never in the
    main repo's — a worktree session must not write into a tree it cannot own."""
    _home(monkeypatch, tmp_path)
    repo, wt = _repo_and_worktree(tmp_path)
    _write_mcp_json(repo, {"from-main": _stdio_server()})

    mgr = MCPManager(str(wt), config_root=str(repo))
    mgr.load()
    try:
        tool = next(t for t in mgr.list_tools()
                    if t.name == "mcp__from-main__echo")
        assert Path(tool.spill_cwd).resolve() == wt.resolve()

        result = asyncio.run(tool.run_safe("c1", text="A" * 50_000))
        assert "characters omitted" in result.content
        spill_dir = wt / ".cluxmate" / "tmp-spill"
        spilled = list(spill_dir.glob("*"))
        assert len(spilled) == 1
        assert str(spilled[0]) in result.content
        assert not (repo / ".cluxmate" / "tmp-spill").exists()
    finally:
        mgr.shutdown()


def test_mcp_manager_without_a_config_root_stays_on_the_cwd(
    tmp_path, monkeypatch
):
    """Default fallback: config_root=None ⇒ config, exec and spill all on cwd."""
    _home(monkeypatch, tmp_path)
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    (proj / ".cluxmate").mkdir(parents=True)
    _write_mcp_json(proj, {"here": _stdio_server()})

    for mgr in (MCPManager(str(proj)), MCPManager(str(proj), config_root=None)):
        mgr.load()
        try:
            assert mgr.config("here") is not None
            assert mgr.client("here")._cwd == str(proj)
            tool = next(t for t in mgr.list_tools() if t.name == "mcp__here__echo")
            assert tool.spill_cwd == str(proj)
        finally:
            mgr.shutdown()
    assert not (home / ".cluxmate" / "mcp.json").exists()


def test_mcp_config_manager_reads_the_root_it_is_given(tmp_path, monkeypatch):
    """MCPConfigManager still takes its root positionally (renamed, not moved)."""
    _home(monkeypatch, tmp_path)
    repo, wt = _repo_and_worktree(tmp_path)
    _write_mcp_json(repo, {"from-main": _stdio_server()})
    _write_mcp_json(wt, {"from-worktree": _stdio_server()})

    assert set(MCPConfigManager(str(repo))._roots()) == {
        Path.home() / ".cluxmate" / "mcp.json",
        repo / ".cluxmate" / "mcp.json",
    }
    assert set(MCPConfigManager(str(repo)).load()) == {"from-main"}
    assert set(MCPConfigManager(str(wt)).load()) == {"from-worktree"}
    assert MCPConfigManager(str(wt), trusted=False).load() == {}


# ── hooks: config root vs exec tree ─────────────────────────────────────────

def test_project_hooks_come_from_the_config_root_but_run_in_the_session_tree(
    tmp_path, monkeypatch
):
    """Hook config is the main repo's; the process cwd and the payload's "cwd"
    are the session tree."""
    _home(monkeypatch, tmp_path)
    repo, wt = _repo_and_worktree(tmp_path)
    script = tmp_path / "hook_helper.py"
    script.write_text(_HELPER, encoding="utf-8")
    _write_settings(repo, {"PreToolUse": [_hook_entry(script)]})
    _write_settings(wt, {"PostToolUse": [_hook_entry(script)]})

    mgr = HookManager(str(wt), trusted=True, config_root=str(repo))
    assert mgr.has_event("PreToolUse") is True
    assert mgr.has_event("PostToolUse") is False

    result = asyncio.run(mgr.run_event("PreToolUse", tool_name="bash"))
    assert len(result.feedback) == 1
    report = json.loads(result.feedback[0])
    assert Path(report["process_cwd"]).resolve() == wt.resolve()
    assert Path(report["payload"]["cwd"]).resolve() == wt.resolve()
    assert Path(report["payload"]["cwd"]).resolve() != repo.resolve()
    assert report["payload"]["hook_event_name"] == "PreToolUse"
    assert report["payload"]["tool_name"] == "bash"


def test_hook_manager_without_a_config_root_stays_on_the_cwd(
    tmp_path, monkeypatch
):
    """Default fallback: config_root=None ⇒ config, cwd and payload all on cwd."""
    _home(monkeypatch, tmp_path)
    proj = tmp_path / "proj"
    (proj / ".cluxmate").mkdir(parents=True)
    script = tmp_path / "hook_helper.py"
    script.write_text(_HELPER, encoding="utf-8")
    _write_settings(proj, {"PreToolUse": [_hook_entry(script)]})

    for mgr in (HookManager(str(proj)), HookManager(str(proj), config_root=None)):
        assert mgr.has_event("PreToolUse") is True
        result = asyncio.run(mgr.run_event("PreToolUse", tool_name="bash"))
        report = json.loads(result.feedback[0])
        assert Path(report["process_cwd"]).resolve() == proj.resolve()
        assert Path(report["payload"]["cwd"]).resolve() == proj.resolve()


# ── the builder's two wiring sites ──────────────────────────────────────────

class _Provider:
    def set_reasoning_effort(self, effort):
        pass


def test_builder_passes_the_exec_tree_and_the_config_root_to_mcp(
    tmp_path, monkeypatch
):
    _home(monkeypatch, tmp_path)
    repo, wt = _repo_and_worktree(tmp_path)
    seen: list[tuple] = []

    class _FakeMCP:
        def __init__(self, cwd, sandbox=None, egress_mode="shared",
                     trusted=True, *, config_root=None):
            seen.append((cwd, config_root))

        def load(self):
            pass

        def list_tools(self):
            return []

    monkeypatch.setattr(builder_mod, "MCPManager", _FakeMCP)
    # _get_tools() is one construction site (the eager one) ...
    eager = (AgentBuilder(str(wt), _Provider())
             .with_default_tools()
             .with_project_root(str(repo)))
    eager._get_tools()
    assert seen == [(str(wt), str(repo))]
    # ... load_mcp() is the other (deferred) one.
    deferred = (AgentBuilder(str(wt), _Provider())
                .with_default_tools()
                .with_project_root(str(repo)))
    deferred.load_mcp()
    assert seen == [(str(wt), str(repo)), (str(wt), str(repo))]


def test_builder_passes_the_exec_tree_and_the_config_root_to_hooks(
    tmp_path, monkeypatch
):
    _home(monkeypatch, tmp_path)
    repo, wt = _repo_and_worktree(tmp_path)
    seen: list[tuple] = []

    class _FakeHooks:
        def __init__(self, cwd, *, trusted=True, config_root=None):
            seen.append((cwd, config_root))

    monkeypatch.setattr(builder_mod, "HookManager", _FakeHooks)
    b = (AgentBuilder(str(wt), _Provider())
         .with_default_tools()
         .with_project_root(str(repo)))
    b._hooks_manager()
    assert seen == [(str(wt), str(repo))]
