"""Tests for LSP config loading and default spec table."""

import json
import sys
from pathlib import Path

import pytest

from cluxmate.core.lsp import LSPConfigManager, LSP_DEFAULT_SPECS, ServerSpec as _ServerSpec


def _write_lsp_json(dir_path: Path, servers: dict) -> None:
    cfg_dir = dir_path / ".cluxmate"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "lsp.json").write_text(
        json.dumps({"servers": servers}, indent=2), encoding="utf-8"
    )


def _write_lsp_raw(dir_path: Path, data: dict) -> None:
    """Write lsp.json verbatim (for top-level keys like auto_install)."""
    cfg_dir = dir_path / ".cluxmate"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "lsp.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


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
    assert LSP_DEFAULT_SPECS["python"].install_cmd == ["npm", "install", "-g", "pyright"]
    assert LSP_DEFAULT_SPECS["rust"].install_cmd == ["rustup", "component", "add", "rust-analyzer"]
    # Platform-specific manual installs stay download-prompt-only.
    assert LSP_DEFAULT_SPECS["java"].install_cmd == []
    assert LSP_DEFAULT_SPECS["cpp"].install_cmd == []


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


def test_top_level_auto_install_defaults_off(tmp_path, monkeypatch):
    home, project = _cfg_with_home(tmp_path, monkeypatch)
    cfg = LSPConfigManager(str(project)).load_config()
    assert cfg.auto_install is False
    assert "python" in cfg.specs


def test_top_level_auto_install_flag(tmp_path, monkeypatch):
    home, project = _cfg_with_home(tmp_path, monkeypatch)
    _write_lsp_raw(project, {"auto_install": True})
    cfg = LSPConfigManager(str(project)).load_config()
    assert cfg.auto_install is True
    assert cfg.specs["python"].auto_install is None  # inherits the top level


def test_per_server_auto_install_override(tmp_path, monkeypatch):
    home, project = _cfg_with_home(tmp_path, monkeypatch)
    _write_lsp_raw(project, {"auto_install": True, "servers": {"python": {"auto_install": False}}})
    cfg = LSPConfigManager(str(project)).load_config()
    assert cfg.auto_install is True
    assert cfg.specs["python"].auto_install is False


def test_install_cmd_accepts_string_or_list(tmp_path, monkeypatch):
    home, project = _cfg_with_home(tmp_path, monkeypatch)
    _write_lsp_json(project, {
        "python": {"install_cmd": "npm i -g pyright"},
        "go": {"install_cmd": ["go", "install", "x@latest"]},
    })
    specs = LSPConfigManager(str(project)).load()
    assert specs["python"].install_cmd == ["npm", "i", "-g", "pyright"]
    assert specs["go"].install_cmd == ["go", "install", "x@latest"]


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


def test_lsp_client_answers_server_request_during_handshake(tmp_path):
    # Regression: the fake server issues a workspace/configuration request
    # mid-initialize (server id=1, colliding with the client's id space) and
    # only completes the handshake once answered. A client that drops
    # server→client requests deadlocks here; start() must return promptly.
    import threading

    client = _lsp_client(tmp_path)
    done = threading.Event()

    def _run():
        try:
            assert client.start() is True
        finally:
            done.set()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    assert done.wait(timeout=10), "start() deadlocked on a server-initiated request"
    try:
        # Handshake completed and id spaces stayed distinct: a normal request
        # still round-trips correctly.
        result = client.request(
            "textDocument/definition",
            {"textDocument": {"uri": _path_to_uri(str(tmp_path / "a.py"))},
             "position": {"line": 0, "character": 0}},
        )
        assert result == [{"uri": "file:///fake/def.py", "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 4}}}]
    finally:
        client.shutdown()


def test_lsp_client_start_raises_not_installed_for_missing_binary(tmp_path):
    from cluxmate.core.lsp import ServerNotInstalledError
    spec = _ServerSpec(
        command="cluxmate-definitely-missing-lsp-xyz",
        extension_to_language={".py": "python"},
        install_hint="install it somehow",
    )
    client = LSPClient(spec, language_id="python", root=str(tmp_path))
    with pytest.raises(ServerNotInstalledError) as ei:
        client.start()
    assert "install it somehow" in str(ei.value)
    assert ei.value.spec is spec


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


def test_manager_new_navigation_ops_roundtrip(tmp_path):
    (tmp_path / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    mgr = _manager(tmp_path)
    try:
        assert "impl.py" in mgr.implementation("a.py", 1, "foo")
        assert "decl.py" in mgr.declaration("a.py", 1, "foo")
        assert "type_def.py" in mgr.type_definition("a.py", 1, "foo")
        incoming = mgr.call_hierarchy("a.py", 1, "foo", "incomingCalls")
        assert "callers of foo" in incoming
        assert "caller.py" in incoming
        outgoing = mgr.call_hierarchy("a.py", 1, "foo", "outgoingCalls")
        assert "callees of foo" in outgoing
        assert "callee.py" in outgoing
        bad = mgr.call_hierarchy("a.py", 1, "foo", "bogus")
        assert "unknown call hierarchy kind" in bad
    finally:
        mgr.shutdown()


def test_manager_call_hierarchy_defaults_to_incoming(tmp_path):
    (tmp_path / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    mgr = _manager(tmp_path)
    try:
        out = mgr.call_hierarchy("a.py", 1, "foo")
        assert "callers of foo" in out
    finally:
        mgr.shutdown()


# --- auto-install ---------------------------------------------------------

import os
import textwrap


def _fake_installer(tmp_path: Path, monkeypatch) -> tuple[Path, str]:
    """A fake install command that "installs" a binary by dropping a file into
    bindir (added to PATH), mirroring how npm/rustup put tools on PATH.

    The dropped file is an inert placeholder, NOT a runnable server: it exists
    on PATH but can never exec. That keeps the resulting query's failure mode
    deterministic on every OS — POSIX raises ENOEXEC and Windows "not a valid
    Win32 application" (both OSError → "failed to start"). A `#!/bin/sh` stub
    instead would spawn and exit cleanly on POSIX, making the failure surface
    only later at initialize with a different message.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    name = "fake-lsp" + (".exe" if os.name == "nt" else "")
    installer = tmp_path / "installer.py"
    installer.write_text(textwrap.dedent("""
        import os, sys
        target = os.path.join(sys.argv[1], sys.argv[2])
        with open(target, "w", encoding="utf-8") as f:
            f.write("not a real lsp server\\n")
        try:
            os.chmod(target, 0o755)
        except OSError:
            pass
        print("installed " + target)
    """), encoding="utf-8")
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    return bindir, name


def _install_spec(bindir: Path, name: str, tmp_path: Path) -> _ServerSpec:
    return _ServerSpec(
        command=name,
        extension_to_language={".py": "python"},
        install_cmd=[sys.executable, str(tmp_path / "installer.py"), str(bindir), name],
    )


def test_run_install_runs_command_and_detects_binary(tmp_path, monkeypatch):
    # _run_install is the auto-install engine: it executes spec.install_cmd
    # (unsandboxed, serialized) and reports success only when the binary
    # resolves on PATH afterwards.
    bindir, name = _fake_installer(tmp_path, monkeypatch)
    spec = _install_spec(bindir, name, tmp_path)
    mgr = LSPManager(str(tmp_path), specs={"python": spec})
    try:
        installed, output = mgr._run_install(spec)
        assert installed is True
        assert (bindir / name).exists()
        assert "installed" in output
    finally:
        mgr.shutdown()


def test_resolve_without_install_cmd_surfaces_hint_only(tmp_path):
    # Download-prompt-only spec (no portable install command, e.g. java/cpp):
    # the tool message carries the manual hint and never tries to install.
    spec = _ServerSpec(
        command="cluxmate-missing-xyz",
        extension_to_language={".py": "python"},
        install_hint="manual steps here",
    )
    (tmp_path / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    mgr = LSPManager(str(tmp_path), specs={"python": spec})
    try:
        out = mgr.definition("a.py", 1, "foo")
        assert "is not installed" in out
        assert "manual steps here" in out
    finally:
        mgr.shutdown()


def test_resolve_missing_server_surfaces_prompt_without_installing(tmp_path, monkeypatch):
    # Specs passed directly → no lsp.json auto_install default, so the missing
    # binary must NOT be auto-installed; the tool surfaces the install prompt.
    bindir, name = _fake_installer(tmp_path, monkeypatch)
    spec = _install_spec(bindir, name, tmp_path)
    spec.install_hint = "hint-here"
    (tmp_path / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    mgr = LSPManager(str(tmp_path), specs={"python": spec})
    try:
        out = mgr.definition("a.py", 1, "foo")
        assert "is not installed" in out
        assert "hint-here" in out
        assert "auto_install" in out
        assert not (bindir / name).exists()
    finally:
        mgr.shutdown()


def test_resolve_auto_installs_when_spec_opts_in(tmp_path, monkeypatch):
    # Per-server auto_install=True (user config) + manager gate on → the
    # missing binary triggers the install command before the spawn. The fake
    # installed binary is not a real LSP server, so the query ends with a
    # start-failure — but the install itself must have run.
    bindir, name = _fake_installer(tmp_path, monkeypatch)
    spec = _install_spec(bindir, name, tmp_path)
    spec.auto_install = True
    (tmp_path / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    mgr = LSPManager(str(tmp_path), specs={"python": spec})
    try:
        out = mgr.definition("a.py", 1, "foo")
        assert (bindir / name).exists()
        assert "failed to start" in out
    finally:
        mgr.shutdown()


def test_plan_mode_gate_blocks_auto_install(tmp_path, monkeypatch):
    bindir, name = _fake_installer(tmp_path, monkeypatch)
    spec = _install_spec(bindir, name, tmp_path)
    spec.auto_install = True
    (tmp_path / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    mgr = LSPManager(str(tmp_path), specs={"python": spec})
    mgr.auto_install = False  # what the builder does in plan mode
    try:
        out = mgr.definition("a.py", 1, "foo")
        assert "is not installed" in out
        assert not (bindir / name).exists()
    finally:
        mgr.shutdown()


# --- wire-framing regression: LSP stdio is Content-Length framed, not NDJSON.
# A naive readline() client blocks forever on a body that has no trailing
# newline; these tests assert byte-exact framing directly (and non-ASCII
# content, which readline/char-count based parsing gets wrong).

import io


class _FakeStream:
    def __init__(self, data: bytes = b""):
        self.buffer = io.BytesIO(data)


class _FakeProc:
    def __init__(self, response: bytes = b""):
        self.stdin = _FakeStream()
        self.stdout = _FakeStream(response)


def _framing_client() -> LSPClient:
    spec = _ServerSpec(command="fake", extension_to_language={".py": "python"})
    return LSPClient(spec, language_id="python", root=".")


def test_write_uses_content_length_framing():
    client = _framing_client()
    client._proc = _FakeProc()
    payload = {"jsonrpc": "2.0", "id": 1, "result": "中文"}
    client._write(payload)
    raw = client._proc.stdin.buffer.getvalue()
    header, _, body = raw.partition(b"\r\n\r\n")
    assert header.startswith(b"Content-Length: ")
    assert int(header.split(b":", 1)[1].strip()) == len(body)
    assert json.loads(body.decode("utf-8")) == payload


def test_read_consumes_exact_bytes():
    client = _framing_client()
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": "中文"}).encode("utf-8")
    frame = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body + b'{"trailing":1}'
    client._proc = _FakeProc(frame)
    msg = client._read()
    assert msg == {"jsonrpc": "2.0", "id": 1, "result": "中文"}
    # only the framed body was consumed; the next frame is still buffered.
    assert client._proc.stdout.buffer.read() == b'{"trailing":1}'


def test_lsp_client_caches_publish_diagnostics(tmp_path):
    # fake_lsp_server 在 didOpen 后主动 push publishDiagnostics（Task 3 补充
    # server 行为；本任务先验证 client 侧的缓存读取接口存在并可用）。
    client = _lsp_client(tmp_path)
    try:
        assert client.start() is True
        uri = _path_to_uri(str(tmp_path / "a.py"))
        # fake server 尚未实现 push，当前先手写一个 Diagnostic 到缓存，验证接口。
        client._diagnostics = {uri: [{"severity": 1, "message": "boom"}]}
        assert client.diagnostics_for(uri) == [{"severity": 1, "message": "boom"}]
        assert client.diagnostics_for("file:///other.py") == []
    finally:
        client.shutdown()


def test_lsp_client_drain_pending_no_hang_without_data(tmp_path):
    # 没有 pending 通知时 drain 必须及时返回（不阻塞整段超时）。
    import time
    client = _lsp_client(tmp_path)
    try:
        assert client.start() is True
        t0 = time.monotonic()
        client.drain_pending(timeout_seconds=0.1)
        assert time.monotonic() - t0 < 1.0
    finally:
        client.shutdown()


def test_lsp_client_captures_pushed_diagnostics(tmp_path):
    client = _lsp_client(tmp_path)
    try:
        assert client.start() is True
        path = tmp_path / "a.py"
        path.write_text("NEEDS_DIAGNOSTICS\ndef foo():\n    pass\n", encoding="utf-8")
        uri = _path_to_uri(str(path))
        client.ensure_synced(uri, str(path))
        client.drain_pending(1.0)
        diags = client.diagnostics_for(uri)
        assert any(d.get("severity") == 1 for d in diags)
        assert any(d.get("severity") == 2 for d in diags)
    finally:
        client.shutdown()


def test_manager_diagnostics_formats_severity_and_location(tmp_path):
    (tmp_path / "a.py").write_text("NEEDS_DIAGNOSTICS\ndef foo():\n    pass\n", encoding="utf-8")
    mgr = _manager(tmp_path)
    try:
        out = mgr.diagnostics("a.py")
        assert "a.py:1:1" in out
        assert "[error]" in out
        assert "[warning]" in out
        assert "fake error" in out
        assert "fake warning" in out
        assert "(fake, E001)" in out
    finally:
        mgr.shutdown()


def test_manager_diagnostics_no_diagnostics(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    mgr = _manager(tmp_path)
    try:
        out = mgr.diagnostics("a.py")
        assert out == "no diagnostics"
    finally:
        mgr.shutdown()
