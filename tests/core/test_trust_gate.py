"""Untrusted ⇒ no project-level config family is loaded.

One matrix per reader, because the gate is a keyword argument each reader must
opt into: a future project-level reader that forgets it is exactly what these
tests are here to catch.
"""

import json
from pathlib import Path

from cluxmate.core.hooks import HookManager
from cluxmate.core.lsp import LSPConfigManager
from cluxmate.core.mcp import MCPConfigManager, MCPManager


def _home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    return home


def _project(tmp_path, monkeypatch) -> Path:
    """A directory that ships one of everything, with an empty global home."""
    _home(tmp_path, monkeypatch)
    cwd = tmp_path / "proj"
    state = cwd / ".cluxmate"
    state.mkdir(parents=True)
    return cwd


def _hooks(path: Path, command: str) -> str:
    return json.dumps(
        {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}}
    )


# ── hooks ────────────────────────────────────────────────────────────────

def test_project_hooks_are_skipped_but_global_ones_still_run(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    (home / ".cluxmate" / "settings.json").write_text(_hooks(home, "echo global"), encoding="utf-8")
    cwd = _project(tmp_path, monkeypatch)
    (cwd / ".cluxmate" / "settings.json").write_text(_hooks(cwd, "echo project"), encoding="utf-8")

    trusted = HookManager(str(cwd))
    assert [s.command for s in trusted._specs["SessionStart"]] == ["echo global", "echo project"]

    untrusted = HookManager(str(cwd), trusted=False)
    assert [s.command for s in untrusted._specs["SessionStart"]] == ["echo global"]


def test_project_only_hooks_leave_the_event_unset(tmp_path, monkeypatch):
    cwd = _project(tmp_path, monkeypatch)
    (cwd / ".cluxmate" / "settings.json").write_text(_hooks(cwd, "echo project"), encoding="utf-8")
    assert HookManager(str(cwd), trusted=False).has_event("SessionStart") is False


# ── MCP ──────────────────────────────────────────────────────────────────

def test_project_mcp_servers_are_not_configured(tmp_path, monkeypatch):
    cwd = _project(tmp_path, monkeypatch)
    (cwd / ".cluxmate" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"evil": {"command": "python", "args": ["-c", "print(1)"]}}}),
        encoding="utf-8",
    )
    assert "evil" in MCPConfigManager(str(cwd)).load()
    assert MCPConfigManager(str(cwd), trusted=False).load() == {}


def test_global_mcp_servers_still_load_when_untrusted(tmp_path, monkeypatch):
    """The gate drops the project root only; the user's own global root stays.

    Without this, a `_roots()` regression to `[]` (or a gate applied to the
    global root too) would leave the whole MCP half of the matrix green.
    """
    home = _home(tmp_path, monkeypatch)
    (home / ".cluxmate" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"global": {"command": "python", "args": []}}}),
        encoding="utf-8",
    )
    cwd = _project(tmp_path, monkeypatch)
    assert set(MCPConfigManager(str(cwd), trusted=False).load()) == {"global"}


def test_untrusted_mcp_load_exposes_no_tools(tmp_path, monkeypatch):
    cwd = _project(tmp_path, monkeypatch)
    (cwd / ".cluxmate" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"evil": {"command": "python", "args": []}}}),
        encoding="utf-8",
    )
    mgr = MCPManager(str(cwd), trusted=False)
    mgr.load()
    try:
        # The tool list alone proves nothing: `evil` is a plain `python` whose
        # initialize handshake fails, so it would expose no tools either way.
        # The config/client maps do discriminate — a manager that failed to
        # forward `trusted` still records the server it read.
        assert mgr._configs == {}
        assert mgr._clients == {}
        assert mgr.list_tools() == []
    finally:
        mgr.shutdown()


# ── LSP ──────────────────────────────────────────────────────────────────

def test_project_lsp_config_is_ignored(tmp_path, monkeypatch):
    cwd = _project(tmp_path, monkeypatch)
    (cwd / ".cluxmate" / "lsp.json").write_text(
        json.dumps(
            {"auto_install": True,
             "servers": {"python": {"command": "evil-ls", "install_cmd": "echo pwn"}}}
        ),
        encoding="utf-8",
    )
    trusted = LSPConfigManager(str(cwd)).load_config()
    assert trusted.specs["python"].command == "evil-ls"

    untrusted = LSPConfigManager(str(cwd), trusted=False).load_config()
    assert untrusted.specs["python"].command != "evil-ls"
    assert untrusted.auto_install is False


def test_global_lsp_servers_still_load_when_untrusted(tmp_path, monkeypatch):
    """Same root-survival check for the LSP reader: global is not gated.

    The untrusted project file in the fixture would otherwise mask a regression
    that made `_roots()` return `[]` for every trust state.
    """
    home = _home(tmp_path, monkeypatch)
    (home / ".cluxmate" / "lsp.json").write_text(
        json.dumps({"servers": {"globallang": {"command": "global-ls"}}}),
        encoding="utf-8",
    )
    cwd = _project(tmp_path, monkeypatch)
    (cwd / ".cluxmate" / "lsp.json").write_text(
        json.dumps({"servers": {"evillang": {"command": "evil-ls"}}}),
        encoding="utf-8",
    )
    specs = LSPConfigManager(str(cwd), trusted=False).load_config().specs
    assert specs["globallang"].command == "global-ls"
    assert "evillang" not in specs


def test_lsp_manager_forwards_the_gate(tmp_path, monkeypatch):
    cwd = _project(tmp_path, monkeypatch)
    (cwd / ".cluxmate" / "lsp.json").write_text(
        json.dumps({"auto_install": True, "servers": {"python": {"command": "evil-ls"}}}),
        encoding="utf-8",
    )
    from cluxmate.core.lsp import LSPManager

    mgr = LSPManager(str(cwd), trusted=False)
    try:
        assert mgr.specs["python"].command != "evil-ls"
        assert mgr._auto_install_default is False
    finally:
        mgr.shutdown()


# ── skills / subagents ───────────────────────────────────────────────────

def test_project_skills_and_agents_are_hidden(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    (home / ".cluxmate" / "skills" / "global-skill").mkdir(parents=True)
    (home / ".cluxmate" / "skills" / "global-skill" / "SKILL.md").write_text(
        "---\nname: global-skill\ndescription: g\n---\nbody\n", encoding="utf-8"
    )
    (home / ".cluxmate" / "agents").mkdir(parents=True)
    cwd = _project(tmp_path, monkeypatch)
    (cwd / ".cluxmate" / "skills" / "proj-skill").mkdir(parents=True)
    (cwd / ".cluxmate" / "skills" / "proj-skill" / "SKILL.md").write_text(
        "---\nname: proj-skill\ndescription: p\n---\nbody\n", encoding="utf-8"
    )
    (cwd / ".cluxmate" / "agents").mkdir(parents=True)
    (cwd / ".cluxmate" / "agents" / "helper.md").write_text(
        "---\ndescription: helper\n---\nbody\n", encoding="utf-8"
    )

    from cluxmate.core.skills import SkillManager
    from cluxmate.core.subagents import SubagentRegistry

    assert {s.slug for s in SkillManager(str(cwd)).discover_enabled()} == {
        "global-skill", "proj-skill",
    }
    assert {s.slug for s in SkillManager(str(cwd), trusted=False).discover_enabled()} == {
        "global-skill",
    }

    assert "helper" in {a["slug"] for a in SubagentRegistry(str(cwd)).snapshot()["agents"]}
    untrusted = SubagentRegistry(str(cwd), trusted=False).snapshot()
    assert "helper" not in {a["slug"] for a in untrusted["agents"]}
    assert "general-purpose" in {a["slug"] for a in untrusted["agents"]}


def test_project_skill_disable_list_is_ignored_when_untrusted(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    (home / ".cluxmate" / "skills" / "demo").mkdir(parents=True)
    (home / ".cluxmate" / "skills" / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    cwd = _project(tmp_path, monkeypatch)
    (cwd / ".cluxmate" / "skills.json").write_text(
        json.dumps({"disabledSkills": ["demo"]}), encoding="utf-8"
    )

    from cluxmate.core.skills import SkillManager

    assert SkillManager(str(cwd)).discover_enabled() == []
    assert [s.slug for s in SkillManager(str(cwd), trusted=False).discover_enabled()] == ["demo"]


# ── permissions ──────────────────────────────────────────────────────────

def test_untrusted_permissions_ignore_always_allow(tmp_path, monkeypatch):
    cwd = _project(tmp_path, monkeypatch)
    (cwd / ".cluxmate" / "permissions.json").write_text(
        json.dumps(
            {"always_allow_tools": ["write_file"],
             "always_allow_dangerous_tools": ["bash:rm", "delete_file"]}
        ),
        encoding="utf-8",
    )

    from cluxmate.core.permissions import PermissionPolicy

    trusted = PermissionPolicy(str(cwd))
    assert trusted.is_auto_approved("write_file", "write") is True
    assert trusted.is_auto_approved("bash", "dangerous", categories=frozenset({"rm"})) is True
    assert trusted.snapshot()["trusted"] is True

    untrusted = PermissionPolicy(str(cwd), trusted=False)
    assert untrusted.is_auto_approved("write_file", "write") is False
    assert untrusted.is_auto_approved("bash", "dangerous", categories=frozenset({"rm"})) is False
    assert untrusted.is_auto_approved("delete_file", "dangerous") is False
    assert untrusted.snapshot()["trusted"] is False


def test_untrusted_permissions_refuse_always_allow_writes(tmp_path, monkeypatch):
    cwd = _project(tmp_path, monkeypatch)

    from cluxmate.core.permissions import PermissionPolicy

    policy = PermissionPolicy(str(cwd), trusted=False)
    assert policy.is_always_allowable("write_file", "write") is False
    policy.add_always_allow("write_file")
    policy.add_always_allow_dangerous("delete_file")
    assert policy.snapshot()["always_allow_tools"] == []
    assert policy.snapshot()["always_allow_dangerous_tools"] == []
    assert not (cwd / ".cluxmate" / "permissions.json").exists()


# ── retrieval facts ──────────────────────────────────────────────────────

def _enabled_retrieval_config(tmp_path) -> "object":
    from cluxmate.core.retrieval_memory import RetrievalConfig

    p = tmp_path / "retrieval-memory.json"
    p.write_text(json.dumps({"enabled": True}), encoding="utf-8")
    return RetrievalConfig(p)


def test_project_facts_are_neither_recalled_nor_written(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    cwd = _project(tmp_path, monkeypatch)
    facts = cwd / ".cluxmate" / "memory" / "facts"
    facts.mkdir(parents=True)
    (facts / "abc123.md").write_text("the zebra protocol uses ports", encoding="utf-8")
    global_facts = home / ".cluxmate" / "memory" / "global" / "facts"
    global_facts.mkdir(parents=True)
    (global_facts / "glob1.md").write_text("the zebra protocol is global", encoding="utf-8")

    from cluxmate.core.retrieval_memory import RetrievalMemory

    cfg = _enabled_retrieval_config(tmp_path)
    trusted = RetrievalMemory(str(cwd), cfg)
    assert "ports" in (trusted.recall("zebra protocol") or "")

    untrusted = RetrievalMemory(str(cwd), cfg, trusted=False)
    recalled = untrusted.recall("zebra protocol") or ""
    assert "ports" not in recalled
    assert "global" in recalled  # the global half still works


def test_untrusted_project_remember_is_refused(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    cwd = _project(tmp_path, monkeypatch)

    from cluxmate.core.retrieval_memory import RetrievalMemory

    untrusted = RetrievalMemory(str(cwd), _enabled_retrieval_config(tmp_path), trusted=False)
    msg = untrusted.remember("something durable", scope="project")
    assert "not trusted" in msg
    assert not (cwd / ".cluxmate" / "memory" / "facts").exists()
    assert "Recorded fact" in untrusted.remember("something durable", scope="global")
