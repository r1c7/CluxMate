"""Tests for builder wiring of the read-denylist store + sensitive template."""

from cluxmate.core.builder import AgentBuilder
from cluxmate.core.read_denies import ReadDenyStore


class _Provider:
    pass


def _builder(tmp_path):
    return AgentBuilder(str(tmp_path), _Provider())


def test_no_store_yields_no_denies(tmp_path):
    b = _builder(tmp_path)
    assert b._read_deny_paths() == []
    assert b._protect_sensitive() is False


def test_effective_paths_flow_through_store(tmp_path):
    store = ReadDenyStore(root=tmp_path)
    user = store.add(str(tmp_path / "hidden"))
    store.set_protect_sensitive(True)
    b = _builder(tmp_path)
    b.with_read_denies(store)
    deny = b._read_deny_paths()
    assert user in deny
    assert b._protect_sensitive() is True


def test_child_builder_inherits_read_denies(tmp_path):
    store = ReadDenyStore(root=tmp_path)
    b = _builder(tmp_path)
    b.with_read_denies(store)
    assert b._child_builder("explore", "child-1")._read_denies is store


def test_read_tool_fence_receives_protect_flag(tmp_path, monkeypatch):
    monkeypatch.setattr("cluxmate.core.builder.sandbox_disabled_by_env", lambda: False)
    store = ReadDenyStore(root=tmp_path)
    store.set_protect_sensitive(True)
    b = AgentBuilder(str(tmp_path), _Provider())
    b.with_default_tools().with_mode("default").with_read_denies(store)
    tool = next(t for t in b._get_tools() if t.name == "read_file")
    assert tool._fence._protect_sensitive is True
    assert tool._fence._deny_paths  # built-in dir defaults joined the roots


def test_read_tool_fence_protect_off_by_default(tmp_path):
    b = AgentBuilder(str(tmp_path), _Provider())
    b.with_default_tools().with_mode("default")
    tool = next(t for t in b._get_tools() if t.name == "read_file")
    assert tool._fence._protect_sensitive is False
    assert tool._fence._deny_paths == []
