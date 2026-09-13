"""agents/list — read-only subagent type catalog for the desktop card."""

from pathlib import Path

from cluxmate.core.jsonrpc_server import JsonRpcServer


def _server() -> JsonRpcServer:
    """Never initialized — the catalog must work without a session."""
    return JsonRpcServer()


def test_agents_snapshot_returns_builtins(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    snap = _server()._agents_snapshot({"cwd": str(tmp_path)})
    assert [a["slug"] for a in snap["agents"]][:2] == ["general-purpose", "explore"]
    assert snap["errors"] == []


def test_agents_snapshot_honours_params_cwd(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    d = tmp_path / ".cluxmate" / "agents"
    d.mkdir(parents=True)
    (d / "reviewer.md").write_text("---\ndescription: review\n---\n", encoding="utf-8")
    snap = _server()._agents_snapshot({"cwd": str(tmp_path)})
    assert "reviewer" in [a["slug"] for a in snap["agents"]]
