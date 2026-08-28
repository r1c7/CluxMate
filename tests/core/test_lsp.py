"""Tests for LSP config loading and default spec table."""

import json
import sys
from pathlib import Path

import pytest

from cluxmate.core.lsp import LSPConfigManager, LSP_DEFAULT_SPECS


def _write_lsp_json(dir_path: Path, servers: dict) -> None:
    cfg_dir = dir_path / ".cluxmate"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "lsp.json").write_text(
        json.dumps({"servers": servers}, indent=2), encoding="utf-8"
    )


def _cfg_with_home(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    project = tmp_path / "project"
    project.mkdir()
    return home, project


def test_default_specs_cover_seven_languages():
    for lang in ("python", "typescript", "javascript", "go", "java", "rust", "cpp"):
        assert lang in LSP_DEFAULT_SPECS
    assert LSP_DEFAULT_SPECS["python"].command == "pyright-langserver"
    assert LSP_DEFAULT_SPECS["python"].extension_to_language[".py"] == "python"
    assert LSP_DEFAULT_SPECS["go"].command == "gopls"


def test_load_returns_defaults_without_any_config(tmp_path, monkeypatch):
    home, project = _cfg_with_home(tmp_path, monkeypatch)
    specs = LSPConfigManager(str(project)).load()
    assert "python" in specs
    assert specs["python"].command == "pyright-langserver"


def test_project_overrides_default_command(tmp_path, monkeypatch):
    home, project = _cfg_with_home(tmp_path, monkeypatch)
    _write_lsp_json(project, {
        "python": {"command": "custom-pyright", "args": ["--stdio"]}
    })
    specs = LSPConfigManager(str(project)).load()
    assert specs["python"].command == "custom-pyright"
    assert specs["python"].args == ["--stdio"]
    assert specs["python"].install_hint == "npm i -g pyright"


def test_disabled_server_is_excluded(tmp_path, monkeypatch):
    home, project = _cfg_with_home(tmp_path, monkeypatch)
    _write_lsp_json(project, {"python": {"disabled": True}})
    specs = LSPConfigManager(str(project)).load()
    assert "python" not in specs


def test_global_and_project_deep_merge(tmp_path, monkeypatch):
    home, project = _cfg_with_home(tmp_path, monkeypatch)
    _write_lsp_json(home, {"python": {"command": "global-pyright"}})
    _write_lsp_json(project, {"python": {"install_hint": "npm i -g pyright@custom"}})
    specs = LSPConfigManager(str(project)).load()
    assert specs["python"].command == "global-pyright"
    assert specs["python"].install_hint == "npm i -g pyright@custom"


def test_unknown_language_added_from_config(tmp_path, monkeypatch):
    home, project = _cfg_with_home(tmp_path, monkeypatch)
    _write_lsp_json(project, {
        "custom": {
            "command": "my-server",
            "extension_to_language": {".foo": "foo"},
            "install_hint": "install my-server",
        }
    })
    specs = LSPConfigManager(str(project)).load()
    assert "custom" in specs
    assert specs["custom"].extension_to_language[".foo"] == "foo"


from cluxmate.core.lsp import LSPClient, _path_to_uri

_FAKE_LSP_SERVER = Path(__file__).parent / "fake_lsp_server.py"


def _lsp_client(tmp_path: Path) -> LSPClient:
    from cluxmate.core.lsp import ServerSpec
    spec = ServerSpec(
        command=sys.executable,
        args=[str(_FAKE_LSP_SERVER)],
        extension_to_language={".py": "python"},
    )
    return LSPClient(spec, language_id="python", root=str(tmp_path))


def test_lsp_client_initialize_and_request(tmp_path):
    client = _lsp_client(tmp_path)
    try:
        assert client.start() is True
        assert client.pos_encoding == "utf-16"
        result = client.request(
            "textDocument/definition",
            {"textDocument": {"uri": _path_to_uri(str(tmp_path / "a.py"))},
             "position": {"line": 0, "character": 0}},
        )
        assert result == [{"uri": "file:///fake/def.py", "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 4}}}]
    finally:
        client.shutdown()


from cluxmate.core.lsp import LSPManager


def _manager(tmp_path: Path) -> LSPManager:
    from cluxmate.core.lsp import ServerSpec
    spec = ServerSpec(
        command=sys.executable,
        args=[str(_FAKE_LSP_SERVER)],
        extension_to_language={".py": "python"},
    )
    return LSPManager(str(tmp_path), specs={"python": spec})


def test_manager_definition_formats_location(tmp_path):
    (tmp_path / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    mgr = _manager(tmp_path)
    try:
        out = mgr.definition("a.py", 1, "foo")
        assert "def.py" in out
        assert ":1" in out
    finally:
        mgr.shutdown()


def test_manager_unknown_extension_errors(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    mgr = _manager(tmp_path)
    try:
        out = mgr.definition("a.txt", 1, "x")
        assert "no language server configured" in out
    finally:
        mgr.shutdown()


def test_manager_workspace_symbol_queries_running_clients(tmp_path):
    mgr = _manager(tmp_path)
    try:
        out = mgr.workspace_symbol("Foo")
        assert out == "no workspace symbols found"
    finally:
        mgr.shutdown()
