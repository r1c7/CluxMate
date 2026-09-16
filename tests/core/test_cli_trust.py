"""cluxmate trust — the pre-approval path for headless/CI use."""

import argparse
import json
from pathlib import Path

from cluxmate import cli


def _home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(cli, "_TRUST_STORE", None)
    return home


def _project(tmp_path) -> Path:
    cwd = tmp_path / "proj"
    (cwd / ".cluxmate").mkdir(parents=True)
    (cwd / ".cluxmate" / "mcp.json").write_text("{}", encoding="utf-8")
    return cwd


def test_add_then_status_then_remove(tmp_path, monkeypatch, capsys):
    home = _home(tmp_path, monkeypatch)
    cwd = _project(tmp_path)

    assert cli.run_trust(argparse.Namespace(action="status", path=str(cwd))) == 0
    assert "unknown" in capsys.readouterr().out

    cli.run_trust(argparse.Namespace(action="add", path=str(cwd)))
    registry = json.loads((home / ".cluxmate" / "trust.json").read_text("utf-8"))
    assert list(registry["folders"].values()) == ["trusted"]

    out = capsys.readouterr().out
    assert "trusted" in out

    cli.run_trust(argparse.Namespace(action="status", path=str(cwd)))
    assert "trusted (registry)" in capsys.readouterr().out

    cli.run_trust(argparse.Namespace(action="remove", path=str(cwd)))
    capsys.readouterr()
    cli.run_trust(argparse.Namespace(action="status", path=str(cwd)))
    assert "unknown" in capsys.readouterr().out


def test_deny_and_list(tmp_path, monkeypatch, capsys):
    from cluxmate.core.trust import canonical

    _home(tmp_path, monkeypatch)
    cwd = _project(tmp_path)
    cli.run_trust(argparse.Namespace(action="deny", path=str(cwd)))
    capsys.readouterr()
    cli.run_trust(argparse.Namespace(action="list", path=None))
    out = capsys.readouterr().out
    assert "denied" in out and canonical(str(cwd)) in out


def test_status_lists_the_findings(tmp_path, monkeypatch, capsys):
    _home(tmp_path, monkeypatch)
    cwd = _project(tmp_path)
    cli.run_trust(argparse.Namespace(action="status", path=str(cwd)))
    out = capsys.readouterr().out
    assert "mcp" in out and "mcp.json" in out


def test_headless_warns_about_an_untrusted_directory(tmp_path, monkeypatch, capsys):
    _home(tmp_path, monkeypatch)
    cwd = _project(tmp_path)
    decision = cli._trust_decision(str(cwd))
    assert decision.gated is True
    cli._warn_untrusted(str(cwd), decision)
    err = capsys.readouterr().err
    assert "not trusted" in err and "cluxmate trust add" in err


def test_headless_is_silent_for_an_empty_directory(tmp_path, monkeypatch, capsys):
    _home(tmp_path, monkeypatch)
    cwd = tmp_path / "plain"
    cwd.mkdir()
    cli._warn_untrusted(str(cwd), cli._trust_decision(str(cwd)))
    assert capsys.readouterr().err == ""
