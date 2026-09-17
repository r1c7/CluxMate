"""项目配置读者拿 project_root，树相关的仍拿 cwd。"""

import asyncio
import json
from pathlib import Path

import pytest

from cluxmate.core import builder as builder_mod
from cluxmate.core.builder import AgentBuilder
from cluxmate.core.memory import MemoryManager
from cluxmate.core.retrieval_memory import RetrievalConfig
from cluxmate.tools._fence import SandboxViolation


class _Provider:
    def set_reasoning_effort(self, effort):
        pass


def _home(monkeypatch, tmp_path) -> Path:
    """Redirect the global config root so a real ~/.cluxmate cannot leak in."""
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    return home


def _fake_mcp(monkeypatch, seen: list[str]) -> None:
    """Record the cwd every MCPManager is built with (no subprocesses)."""

    class _FakeMCP:
        def __init__(self, cwd, sandbox=None, egress_mode="shared",
                     trusted=True):
            seen.append(cwd)

        def load(self):
            pass

        def list_tools(self):
            return []

    monkeypatch.setattr(builder_mod, "MCPManager", _FakeMCP)


def _repo_and_worktree(tmp_path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    wt = repo / ".worktrees" / "me"
    (repo / ".cluxmate").mkdir(parents=True)
    wt.mkdir(parents=True)
    return repo, wt


def test_project_root_defaults_to_cwd(tmp_path):
    b = AgentBuilder(str(tmp_path), _Provider())
    assert b.project_root == str(tmp_path)


def test_project_root_is_settable_and_chainable(tmp_path):
    b = AgentBuilder(str(tmp_path / "wt"), _Provider())
    assert b.with_project_root(str(tmp_path)) is b
    assert b.project_root == str(tmp_path)
    assert b.cwd == str(tmp_path / "wt")


def test_project_memory_is_read_from_the_project_root(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    wt = repo / ".worktrees" / "me"
    wt.mkdir(parents=True)
    (repo / "AGENTS.md").write_text("# main repo memory\n", encoding="utf-8")
    (wt / "AGENTS.md").write_text("# worktree memory\n", encoding="utf-8")
    b = AgentBuilder(str(wt), _Provider()).with_project_root(str(repo))
    text = dict(b.render_injections())["memory"]
    assert "main repo memory" in text
    assert "worktree memory" not in text


def test_update_memory_tool_writes_the_project_root(tmp_path):
    import asyncio

    from cluxmate.tools.update_memory import UpdateMemoryTool

    repo = tmp_path / "repo"
    wt = repo / ".worktrees" / "me"
    wt.mkdir(parents=True)
    tool = UpdateMemoryTool(cwd=str(wt), project_root=str(repo))
    asyncio.run(tool.execute(content="hello", scope="project"))
    assert (repo / "AGENTS.md").is_file()
    assert not (wt / "AGENTS.md").exists()


def test_child_builder_inherits_the_project_root(tmp_path):
    from cluxmate.core.subagents import SubagentRegistry

    repo = tmp_path / "repo"
    wt = repo / ".worktrees" / "me"
    wt.mkdir(parents=True)
    b = AgentBuilder(str(wt), _Provider()).with_project_root(str(repo))
    profile = SubagentRegistry(str(repo), trusted=True).get("explore")
    child = b._child_builder(profile, "sub-1")
    assert child.project_root == str(repo)
    assert child.cwd == str(wt)


# ── the remaining rows of the 7-swap table ──────────────────────────────────


def test_project_skills_are_read_from_the_project_root(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    repo, wt = _repo_and_worktree(tmp_path)
    main = repo / ".cluxmate" / "skills" / "demo"
    main.mkdir(parents=True)
    (main / "SKILL.md").write_text(
        "---\nname: Demo\ndescription: ships from the main repo\n---\nbody\n",
        encoding="utf-8",
    )
    tree = wt / ".cluxmate" / "skills" / "wt-only"
    tree.mkdir(parents=True)
    (tree / "SKILL.md").write_text(
        "---\nname: WT\ndescription: only in the worktree\n---\nbody\n",
        encoding="utf-8",
    )
    b = AgentBuilder(str(wt), _Provider()).with_project_root(str(repo))
    skills = dict(b.render_injections())["skill"]
    assert "demo" in skills
    assert "wt-only" not in skills


def test_subagent_types_are_read_from_the_project_root(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    repo, wt = _repo_and_worktree(tmp_path)
    (repo / ".cluxmate" / "agents").mkdir(parents=True)
    (repo / ".cluxmate" / "agents" / "from-main.md").write_text(
        "---\ndescription: declared in the main repo\ntools: [read_file]\n---\nbody\n",
        encoding="utf-8",
    )
    (wt / ".cluxmate" / "agents").mkdir(parents=True)
    (wt / ".cluxmate" / "agents" / "from-wt.md").write_text(
        "---\ndescription: only in the worktree\ntools: [read_file]\n---\nbody\n",
        encoding="utf-8",
    )
    b = AgentBuilder(str(wt), _Provider()).with_project_root(str(repo))
    assert b.agent_type("from-main") is not None
    assert b.agent_type("from-wt") is None


def test_hooks_are_read_from_the_project_root(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    repo, wt = _repo_and_worktree(tmp_path)
    (repo / ".cluxmate" / "settings.json").write_text(json.dumps({"hooks": {
        "PreToolUse": [{"hooks": [{"type": "command", "command": "echo main"}]}],
    }}), encoding="utf-8")
    (wt / ".cluxmate").mkdir(parents=True)
    (wt / ".cluxmate" / "settings.json").write_text(json.dumps({"hooks": {
        "PostToolUse": [{"hooks": [{"type": "command", "command": "echo wt"}]}],
    }}), encoding="utf-8")
    b = AgentBuilder(str(wt), _Provider()).with_project_root(str(repo))
    hooks = b._hooks_manager()
    assert hooks.has_event("PreToolUse") is True
    assert hooks.has_event("PostToolUse") is False


def test_mcp_manager_is_built_from_the_project_root(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    repo, wt = _repo_and_worktree(tmp_path)
    seen: list[str] = []
    _fake_mcp(monkeypatch, seen)
    b = (AgentBuilder(str(wt), _Provider())
         .with_default_tools()
         .with_project_root(str(repo)))
    b._get_tools()
    assert seen == [str(repo)]


def test_deferred_mcp_load_uses_the_project_root(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    repo, wt = _repo_and_worktree(tmp_path)
    seen: list[str] = []
    _fake_mcp(monkeypatch, seen)
    b = (AgentBuilder(str(wt), _Provider())
         .with_default_tools()
         .with_project_root(str(repo)))
    # No _get_tools() first: this exercises load_mcp's OWN construction site.
    b.load_mcp()
    assert seen == [str(repo)]


def test_retrieval_memory_is_built_from_the_project_root(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    repo, wt = _repo_and_worktree(tmp_path)
    cfg = tmp_path / "retrieval-memory.json"
    cfg.write_text(json.dumps({"enabled": True}), encoding="utf-8")
    b = (AgentBuilder(str(wt), _Provider())
         .with_project_root(str(repo))
         .with_retrieval_memory(RetrievalConfig(cfg)))
    mgr = b._retrieval_manager()
    assert mgr is not None
    assert (mgr._facts_dir("project").resolve()
            == (repo / ".cluxmate" / "memory" / "facts").resolve())


def test_write_fence_is_scoped_to_the_project_root(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    repo, wt = _repo_and_worktree(tmp_path)
    b = (AgentBuilder(str(wt), _Provider())
         .with_default_tools()
         .with_project_root(str(repo)))
    fence = next(t for t in b._get_tools() if t.name == "write_file")._fence
    # The project's own config dir is unreachable even though the whole repo
    # sits inside the platform temp root (which IS writable).
    with pytest.raises(SandboxViolation):
        fence.check(repo / ".cluxmate" / "permissions.json")
    # Sibling worktrees are denied; the session's own tree stays writable.
    sibling = repo / ".worktrees" / "other"
    sibling.mkdir()
    with pytest.raises(SandboxViolation):
        fence.check(sibling / "note.txt")
    assert fence.check(wt / "note.txt")


# ── the skill path: advertised AND loadable from the same root ──────────────


def _project_skill(root: Path, slug: str, body: str) -> None:
    d = root / ".cluxmate" / "skills" / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {slug}\ndescription: d\n---\n{body}", encoding="utf-8"
    )


def test_use_skill_loads_a_skill_the_worktree_session_was_advertised(
    tmp_path, monkeypatch
):
    """The tool must read the root the injection lists from: a worktree session
    is advertised the main repo's skills, so `use_skill` has to load them from
    there rather than from the session cwd."""
    _home(monkeypatch, tmp_path)
    repo, wt = _repo_and_worktree(tmp_path)
    _project_skill(repo, "demo", "MAIN BODY")
    _project_skill(wt, "wt-only", "WT BODY")
    b = (AgentBuilder(str(wt), _Provider())
         .with_default_tools()
         .with_project_root(str(repo)))

    injections = dict(b.render_injections())
    assert "demo" in injections["skill"]
    assert "wt-only" not in injections["skill"]

    tool = next(t for t in b._get_tools() if t.name == "use_skill")
    result = asyncio.run(tool.execute(name="demo"))
    assert "MAIN BODY" in result
    # ... and the session's own tree is not a skill root any more: nothing may
    # leak in from the worktree, not even as an "available" hint.
    withheld = asyncio.run(tool.execute(name="wt-only"))
    assert "no skill named 'wt-only'" in withheld
    assert "WT BODY" not in withheld


def test_plan_mode_use_skill_loads_from_the_project_root(tmp_path, monkeypatch):
    """The plan-mode read-tools branch builds its own gate + SkillTool."""
    _home(monkeypatch, tmp_path)
    repo, wt = _repo_and_worktree(tmp_path)
    _project_skill(repo, "demo", "MAIN BODY")
    b = (AgentBuilder(str(wt), _Provider())
         .with_default_tools()
         .with_mode("plan")
         .with_project_root(str(repo)))

    tool = next(t for t in b._get_tools() if t.name == "use_skill")
    assert "MAIN BODY" in asyncio.run(tool.execute(name="demo"))


def test_skill_path_without_a_project_root_stays_on_the_cwd(tmp_path, monkeypatch):
    """A branch built without a project root (the default) is unchanged: the
    gate and the tool keep reading the session cwd, and no other tree leaks in."""
    _home(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    (repo / ".cluxmate").mkdir(parents=True)
    _project_skill(repo, "demo", "MAIN BODY")
    everything = AgentBuilder(str(repo), _Provider()).with_default_tools()
    assert everything.project_root == str(repo)
    tool = next(t for t in everything._get_tools() if t.name == "use_skill")
    assert "MAIN BODY" in asyncio.run(tool.execute(name="demo"))

    # A different session with no project root must not see repo's skills.
    other = tmp_path / "other"
    (other / ".cluxmate").mkdir(parents=True)
    isolated = AgentBuilder(str(other), _Provider()).with_default_tools()
    assert "use_skill" not in {t.name for t in isolated._get_tools()}


def test_builder_wires_update_memory_to_the_project_root(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    repo, wt = _repo_and_worktree(tmp_path)
    b = (AgentBuilder(str(wt), _Provider())
         .with_default_tools()
         .with_project_root(str(repo)))
    tool = next(t for t in b._get_tools() if t.name == "update_memory")
    asyncio.run(tool.execute(content="hello from the worktree", scope="project"))
    assert (repo / "AGENTS.md").is_file()
    assert not (wt / "AGENTS.md").exists()
