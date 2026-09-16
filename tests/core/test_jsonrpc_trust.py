"""Project trust over JSON-RPC: the contract, the notification, and the gate."""

import json
from pathlib import Path

from cluxmate.core import jsonrpc_server
from cluxmate.core.jsonrpc_server import JsonRpcServer
from cluxmate.core.session_log import SessionHeader, SessionLog


class _Provider:
    def set_reasoning_effort(self, effort):
        pass


class _Emitter:
    """Stands in for the stdout writer, recording every outgoing payload."""

    def __init__(self, monkeypatch):
        self.payloads: list[dict] = []
        monkeypatch.setattr(jsonrpc_server, "_write_dict", self)

    def __call__(self, payload: dict):
        self.payloads.append(payload)

    def methods(self) -> list[str]:
        return [p.get("method", "") for p in self.payloads]

    def result_for(self, req_id) -> dict:
        for p in self.payloads:
            if p.get("id") == req_id and "result" in p:
                return p["result"]
        raise AssertionError(f"no result for id={req_id!r}: {self.payloads}")


def _server(tmp_path, monkeypatch, *, ship_config: bool = True) -> JsonRpcServer:
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    # The directory WITHOUT project config must be its own directory: the same
    # tmp_path is reused by a second _server() call, whose .cluxmate/ would
    # otherwise still ship the first call's settings.json.
    cwd = tmp_path / ("proj" if ship_config else "empty")
    (cwd / ".cluxmate").mkdir(parents=True, exist_ok=True)
    if ship_config:
        (cwd / ".cluxmate" / "settings.json").write_text(
            json.dumps({"hooks": {"SessionStart": [
                {"hooks": [{"type": "command", "command": "echo project"}]}]}}),
            encoding="utf-8",
        )

    monkeypatch.setattr(JsonRpcServer, "_build_provider",
                        lambda self, model_id: (_Provider(), "test", False,
                                                {"id": "test", "model_name": "test"}))
    monkeypatch.setattr(JsonRpcServer, "_load_or_create_log",
                        lambda self, session_id, entry: (
                            SessionLog.create(SessionHeader(id="s1", createdAt=0)), True))
    monkeypatch.setattr(JsonRpcServer, "_bind_persister", lambda self: None)
    s = JsonRpcServer()
    s._cwd = str(cwd)
    return s


def test_trust_get_reports_the_findings_and_the_registry(tmp_path, monkeypatch):
    _Emitter(monkeypatch)
    s = _server(tmp_path, monkeypatch)
    result = s._trust_get({"cwd": s._cwd})
    assert result["status"] == "unknown"
    assert result["source"] == "default"
    assert [f["kind"] for f in result["findings"]] == ["hooks"]
    assert result["store"] == {}


def test_trust_set_persists_and_remove_reverts(tmp_path, monkeypatch):
    _Emitter(monkeypatch)
    s = _server(tmp_path, monkeypatch)
    from cluxmate.core.trust import canonical

    assert s._trust_set({"cwd": s._cwd, "status": "trusted"})["status"] == "trusted"
    registry = Path.home() / ".cluxmate" / "trust.json"
    assert registry.is_file()
    assert s._trust_set({"cwd": s._cwd, "status": "denied", "persist": False})["status"] == "denied"
    # The session-only denial did not overwrite (nor add to) the persisted file.
    assert json.loads(registry.read_text("utf-8"))["folders"] == {canonical(s._cwd): "trusted"}
    # remove() drops the registry entry only — this run's override stands, so the
    # directory is still denied (it goes back to undecided once the override is
    # gone too, i.e. in the next process).
    removed = s._trust_remove({"cwd": s._cwd})
    assert removed["status"] == "denied"
    assert removed["source"] == "session"
    assert removed["store"] == {}


def test_a_persisted_set_supersedes_this_runs_override(tmp_path, monkeypatch):
    """The override is resolved before the registry, so a persisted answer has to
    clear it — otherwise persisting a revocation still reports the old
    session-only grant as trusted."""
    _Emitter(monkeypatch)
    s = _server(tmp_path, monkeypatch)
    from cluxmate.core.trust import UNKNOWN

    granted = s._trust_set({"cwd": s._cwd, "status": "trusted", "persist": False})
    assert granted["status"] == "trusted"
    assert granted["source"] == "session"

    revoked = s._trust_set({"cwd": s._cwd, "status": "denied"})
    assert revoked["status"] == "denied"
    assert revoked["source"] == "registry"
    assert s._trust_store.session_status(s._cwd) == UNKNOWN
    # And it stays that way on a later read of the same directory.
    assert s._trust_get({"cwd": s._cwd})["status"] == "denied"


def test_initialize_announces_an_undecided_directory(tmp_path, monkeypatch):
    emitter = _Emitter(monkeypatch)
    s = _server(tmp_path, monkeypatch)
    s._handle_initialize(1, {"session_id": "s1", "cwd": s._cwd})
    assert "trust/required" in emitter.methods()
    assert emitter.result_for(1)["trust"]["status"] == "unknown"


def test_initialize_does_not_announce_a_trusted_or_empty_directory(tmp_path, monkeypatch):
    emitter = _Emitter(monkeypatch)
    s = _server(tmp_path, monkeypatch)
    s._handle_initialize(1, {"session_id": "s1", "cwd": s._cwd, "trust": "trusted"})
    assert "trust/required" not in emitter.methods()
    assert emitter.result_for(1)["trust"]["status"] == "trusted"

    emitter = _Emitter(monkeypatch)
    s2 = _server(tmp_path, monkeypatch, ship_config=False)
    s2._handle_initialize(1, {"session_id": "s1", "cwd": s2._cwd})
    assert "trust/required" not in emitter.methods()


def test_undecided_directory_refuses_chat_send(tmp_path, monkeypatch):
    emitter = _Emitter(monkeypatch)
    s = _server(tmp_path, monkeypatch)
    s._handle_initialize(1, {"session_id": "s1", "cwd": s._cwd})
    s._handle_chat_send(2, {"message": "hello"})
    errors = [p for p in emitter.payloads if p.get("id") == 2 and "error" in p]
    assert errors and "[trust decision required]" in errors[0]["error"]["message"]


def test_deciding_trust_unlocks_the_turn_and_loads_project_hooks(tmp_path, monkeypatch):
    emitter = _Emitter(monkeypatch)
    s = _server(tmp_path, monkeypatch)
    s._handle_initialize(1, {"session_id": "s1", "cwd": s._cwd})
    assert s._builder.trusted is False
    assert s._builder._hooks_manager().has_event("SessionStart") is False

    s._trust_set({"cwd": s._cwd, "status": "trusted"})
    s._handle_initialize(3, {"session_id": "s1", "cwd": s._cwd})
    assert s._builder.trusted is True
    assert s._builder._hooks_manager().has_event("SessionStart") is True


def test_the_rpcs_answer_under_their_colon_alias_and_fall_back_to_the_session_cwd(
    tmp_path, monkeypatch,
):
    """The desktop speaks both spellings; with no `cwd` in params the call is
    about the session's directory, never the bridge process's."""
    emitter = _Emitter(monkeypatch)
    s = _server(tmp_path, monkeypatch)
    s._dispatch(7, "trust:get", {})
    assert emitter.result_for(7)["cwd"] == s._cwd
    assert emitter.result_for(7)["status"] == "unknown"

    s._dispatch(8, "trust:set", {"status": "trusted"})
    assert emitter.result_for(8)["status"] == "trusted"

    s._dispatch(9, "trust:remove", {})
    assert emitter.result_for(9)["store"] == {}


def test_agents_list_hides_the_project_agents_until_trusted(tmp_path, monkeypatch):
    """agents/list resolves the decision for its own cwd, so the desktop's
    `task` card never offers a project subagent the gate withheld."""
    _Emitter(monkeypatch)
    s = _server(tmp_path, monkeypatch)
    d = Path(s._cwd) / ".cluxmate" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "project-only.md").write_text("---\ndescription: x\n---\n", encoding="utf-8")

    slugs = [a["slug"] for a in s._agents_snapshot({})["agents"]]
    assert "project-only" not in slugs

    s._trust_set({"cwd": s._cwd, "status": "trusted"})
    slugs = [a["slug"] for a in s._agents_snapshot({})["agents"]]
    assert "project-only" in slugs
