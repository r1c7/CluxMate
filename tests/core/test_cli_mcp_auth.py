"""CLI surface for MCP OAuth. Runs the real flow against the fake AS."""

import json
import threading
import urllib.request
import webbrowser
from pathlib import Path

import pytest

from cluxmate.cli import run_mcp
from cluxmate.core.mcp_auth_store import MCPAuthStore
from tests.core.fake_mcp_http_server import FakeOAuthServer


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture
def fake():
    servers: list[FakeOAuthServer] = []

    def _make(**knobs) -> FakeOAuthServer:
        s = FakeOAuthServer(**knobs)
        s.start()
        servers.append(s)
        return s

    yield _make
    for s in servers:
        s.stop()


def _write_config(tmp_path, mcp_url: str, extra: dict | None = None) -> str:
    cwd = tmp_path / "proj"
    (cwd / ".cluxmate").mkdir(parents=True)
    entry = {"url": mcp_url}
    entry.update(extra or {})
    (cwd / ".cluxmate" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"remote": entry}}), encoding="utf-8")
    return str(cwd)


def _browser(server: FakeOAuthServer):
    def _open(url: str, *a, **kw) -> bool:
        def _go():
            try:
                urllib.request.urlopen(url, timeout=5)
            except Exception:
                pass
        threading.Thread(target=_go, daemon=True).start()
        return True
    return _open


def test_auth_stores_credentials(tmp_path, monkeypatch, fake):
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    server = fake()
    monkeypatch.setattr(webbrowser, "open", _browser(server))
    cwd = _write_config(tmp_path, server.mcp_url)
    rc = run_mcp(_Args(mcp_command="auth", name="remote", cwd=cwd,
                       callback_port=None, no_browser=False, timeout=5.0, json=False))
    assert rc == 0
    assert MCPAuthStore().get("remote", server.mcp_url) is not None


def test_auth_unknown_server_is_nonzero(tmp_path, monkeypatch, fake, capsys):
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    server = fake(require_bearer=False)
    cwd = _write_config(tmp_path, server.mcp_url)
    rc = run_mcp(_Args(mcp_command="auth", name="nope", cwd=cwd, callback_port=None,
                       no_browser=True, timeout=2.0, json=False))
    assert rc == 1
    assert "nope" in capsys.readouterr().err


def test_auth_reports_the_url_when_the_browser_is_off(tmp_path, monkeypatch, fake, capsys):
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    server = fake()
    cwd = _write_config(tmp_path, server.mcp_url)
    rc = run_mcp(_Args(mcp_command="auth", name="remote", cwd=cwd, callback_port=None,
                       no_browser=True, timeout=1.0, json=False))
    assert rc == 1
    captured = capsys.readouterr()
    assert f"{server.base_url}/authorize?" in captured.out
    assert "timed out" in captured.err.lower()


def test_logout(tmp_path, monkeypatch, fake):
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    server = fake()
    monkeypatch.setattr(webbrowser, "open", _browser(server))
    cwd = _write_config(tmp_path, server.mcp_url)
    assert run_mcp(_Args(mcp_command="auth", name="remote", cwd=cwd, callback_port=None,
                         no_browser=False, timeout=5.0, json=False)) == 0
    assert run_mcp(_Args(mcp_command="logout", name="remote", cwd=cwd, json=False)) == 0
    assert MCPAuthStore().get("remote", server.mcp_url) is None


def test_status_json(tmp_path, monkeypatch, fake, capsys):
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    server = fake(require_bearer=False)
    cwd = _write_config(tmp_path, server.mcp_url)
    assert run_mcp(_Args(mcp_command="status", cwd=cwd, json=True)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out[0]["name"] == "remote"
    assert out[0]["authenticated"] is False
