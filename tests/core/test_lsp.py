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


# --- wire-framing regression: LSP stdio is Content-Length framed, not NDJSON.
# A naive readline() client blocks forever on a body that has no trailing
# newline; these tests assert byte-exact framing directly (and non-ASCII
# content, which readline/char-count based parsing gets wrong).

import io

from cluxmate.core.lsp import ServerSpec as _ServerSpec


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
