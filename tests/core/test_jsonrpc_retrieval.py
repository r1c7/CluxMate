"""Tests for wiring RetrievalConfig into JSON-RPC initialize."""

from pathlib import Path

from cluxmate.core.jsonrpc_server import JsonRpcServer
from cluxmate.core.retrieval_memory import RetrievalConfig
from cluxmate.core.session_log import SessionHeader, SessionLog


class _Provider:
    def set_reasoning_effort(self, effort):
        pass


def test_initialize_wires_retrieval_config(tmp_path, monkeypatch):
    # Redirect ~ before constructing the server so SessionLogStore() and every
    # ~/.cluxmate/*.json config load are scoped to the temp home.
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    cwd = tmp_path / "proj"
    cwd.mkdir()

    s = JsonRpcServer()

    def _build_provider(self, model_id):
        entry = {"id": "test", "model_name": "test", "provider": "test"}
        return _Provider(), "test", False, entry

    def _load_or_create_log(self, session_id, entry):
        return SessionLog.create(SessionHeader(id="s1", createdAt=0)), True

    def _bind_persister(self):
        pass

    monkeypatch.setattr(JsonRpcServer, "_build_provider", _build_provider)
    monkeypatch.setattr(JsonRpcServer, "_load_or_create_log", _load_or_create_log)
    monkeypatch.setattr(JsonRpcServer, "_bind_persister", _bind_persister)

    s._handle_initialize(1, {"session_id": "s1", "cwd": str(cwd)})

    assert isinstance(s._retrieval_config, RetrievalConfig)
    assert s._builder._retrieval_config is s._retrieval_config
