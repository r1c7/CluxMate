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
