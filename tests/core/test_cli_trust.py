"""cluxmate trust — the pre-approval path for headless/CI use."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import pytest

from cluxmate import cli
from cluxmate.core.builder import AgentBuilder
from cluxmate.core.providers.base import LLMResponse


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


# ── the `mcp` subcommand ──────────────────────────────────────────────────
# `cluxmate mcp` reads mcp.json outside any session (no bridge, no agent), so
# the gate has to be applied at this call site too — otherwise an untrusted
# repository gets its servers listed and its URL probed.


def _mcp_args(**kw) -> argparse.Namespace:
    base = {"mcp_command": "status", "name": None, "cwd": None, "json": False}
    base.update(kw)
    return argparse.Namespace(**base)


def _ship_a_server(cwd: Path) -> None:
    (cwd / ".cluxmate" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"proj-server": {"url": "https://repo.example/mcp"}}}),
        encoding="utf-8",
    )


def test_mcp_status_hides_a_project_server_until_trusted(tmp_path, monkeypatch, capsys):
    _home(tmp_path, monkeypatch)
    cwd = _project(tmp_path)
    _ship_a_server(cwd)

    assert cli.run_mcp(_mcp_args(cwd=str(cwd))) == 0
    captured = capsys.readouterr()
    assert "proj-server" not in captured.out
    assert "mcp.json" in captured.err and "cluxmate trust add" in captured.err

    cli.run_trust(argparse.Namespace(action="add", path=str(cwd)))
    capsys.readouterr()
    assert cli.run_mcp(_mcp_args(cwd=str(cwd))) == 0
    captured = capsys.readouterr()
    assert "proj-server" in captured.out
    assert captured.err == ""


def test_mcp_auth_refuses_a_server_from_an_untrusted_project(tmp_path, monkeypatch, capsys):
    """`auth` probes the URL it reads — an untrusted file's URL must not be probed."""
    _home(tmp_path, monkeypatch)
    cwd = _project(tmp_path)
    _ship_a_server(cwd)

    assert cli.run_mcp(_mcp_args(mcp_command="auth", name="proj-server", cwd=str(cwd))) == 1
    err = capsys.readouterr().err
    assert "unknown MCP server" in err
    assert "mcp.json" in err


def test_the_mcp_notice_stays_quiet_without_a_project_mcp_json(tmp_path, monkeypatch, capsys):
    _home(tmp_path, monkeypatch)
    cwd = tmp_path / "plain"
    cwd.mkdir()
    assert cli.run_mcp(_mcp_args(cwd=str(cwd))) == 0
    assert capsys.readouterr().err == ""


# ── argparse wiring ───────────────────────────────────────────────────────
# Hand-built Namespaces skip the subparser, its `choices` and the dispatch, so
# these drive the real front door with sys.argv.


def _main_argv(monkeypatch, argv: list[str]) -> SystemExit:
    monkeypatch.setattr(sys, "argv", ["cluxmate", *argv])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    return exc.value


def test_main_trust_add_writes_through_the_subparser(tmp_path, monkeypatch, capsys):
    home = _home(tmp_path, monkeypatch)
    cwd = _project(tmp_path)

    assert _main_argv(monkeypatch, ["trust", "add", str(cwd)]).code == 0

    registry = json.loads((home / ".cluxmate" / "trust.json").read_text("utf-8"))
    assert list(registry["folders"].values()) == ["trusted"]
    assert f"trusted: {cwd}" in capsys.readouterr().out


def test_main_trust_defaults_to_list(tmp_path, monkeypatch, capsys):
    _home(tmp_path, monkeypatch)

    assert _main_argv(monkeypatch, ["trust"]).code == 0
    assert "No trust decisions recorded." in capsys.readouterr().out


# ── the answer applies to the run that gave it ────────────────────────────


class _Provider:
    """Minimal provider — only the shape the builder queries before a turn."""

    def max_tokens(self) -> int:
        return 1000

    def set_reasoning_effort(self, effort) -> None:
        pass

    async def chat(self, messages, tools, *, on_delta=None, on_thinking=None):
        return LLMResponse(text="ok", stop_reason="end_turn")

    def assistant_message_to_api(self, msg) -> dict:
        return {"role": "assistant", "content": msg.text or ""}

    def tool_result_to_api(self, result) -> dict:
        return {"role": "tool", "tool_call_id": result.tool_call_id,
                "content": result.content}


class _RecordingBuilder(AgentBuilder):
    """The real builder, so the assertions cover every reader it feeds."""

    created: list["_RecordingBuilder"] = []

    def __init__(self, cwd, provider):
        super().__init__(cwd, provider)
        self.trusts = []
        _RecordingBuilder.created.append(self)

    def with_trust(self, decision):
        self.trusts.append(decision)
        super().with_trust(decision)
        return self


class _Tty:
    """A stdin that claims to be a terminal, so the REPL prompt runs."""

    def isatty(self) -> bool:
        return True


def _repl_with_answers(tmp_path, monkeypatch, answers: list[str]) -> None:
    """One `cli.run_repl()` pass: the trust answer first, then `/exit`."""
    _home(tmp_path, monkeypatch)
    cwd = _project(tmp_path)
    monkeypatch.chdir(cwd)
    import cluxmate.core.providers.factory as factory

    monkeypatch.setattr(factory, "build_provider", lambda entry: _Provider())
    monkeypatch.setattr(
        cli, "_resolve_entry",
        lambda model_id=None: {"provider": "p", "model_name": "m"},
    )
    monkeypatch.setattr(cli, "AgentBuilder", _RecordingBuilder)
    monkeypatch.setattr(sys, "stdin", _Tty())
    pending = list(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": pending.pop(0))

    _RecordingBuilder.created = []
    asyncio.run(cli.run_repl())


def test_repl_applies_the_answer_it_just_received(tmp_path, monkeypatch, capsys):
    """Answer `1` must load the project config in the very run that granted it."""
    _repl_with_answers(tmp_path, monkeypatch, ["1", "/exit"])

    builder = _RecordingBuilder.created[-1]
    assert builder.trusts[-1].trusted is True
    # Not just the argument: the readers the builder hands the flag to.
    assert builder.trusted is True
    assert builder._hooks_manager()._trusted is True
    assert "Project config: NOT loaded" not in capsys.readouterr().out


def test_repl_session_answer_applies_without_touching_the_registry(
    tmp_path, monkeypatch, capsys
):
    _repl_with_answers(tmp_path, monkeypatch, ["2", "/exit"])

    builder = _RecordingBuilder.created[-1]
    assert builder.trusts[-1].trusted is True  # "this run only" is not a no-op
    assert builder.trusted is True
    assert builder.trust_source == "session"
    assert not (tmp_path / "home" / ".cluxmate" / "trust.json").exists()


def test_repl_deny_answer_withholds_the_project_config(tmp_path, monkeypatch, capsys):
    _repl_with_answers(tmp_path, monkeypatch, ["3", "/exit"])

    builder = _RecordingBuilder.created[-1]
    assert builder.trusts[-1].status == "denied"
    assert builder.trusted is False
    assert builder.trust_source == "registry"
    assert "Project config: NOT loaded" in capsys.readouterr().out
