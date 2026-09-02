"""Tests for builder wiring of EgressConfig + the proxy lifecycle."""

from cluxmate.core.builder import AgentBuilder
from cluxmate.core.egress_config import EgressConfig


class _Provider:
    pass


def _builder(tmp_path):
    return AgentBuilder(str(tmp_path), _Provider())


def test_with_egress_stores_config(tmp_path):
    cfg = EgressConfig(path=tmp_path / "egress.json")
    b = _builder(tmp_path)
    b.with_egress(cfg)
    assert b._egress is cfg


def test_child_builder_inherits_egress_config(tmp_path):
    cfg = EgressConfig(path=tmp_path / "egress.json")
    b = _builder(tmp_path)
    b.with_egress(cfg)
    assert b._child_builder("explore", "child-1")._egress is cfg


def test_default_egress_mode_is_shared(tmp_path):
    assert _builder(tmp_path)._egress_mode() == "shared"


def test_ensure_egress_proxy_shared_returns_none(tmp_path):
    assert _builder(tmp_path)._ensure_egress_proxy() is None


def test_ensure_egress_proxy_starts_and_stops(tmp_path):
    cfg = EgressConfig(path=tmp_path / "egress.json")
    cfg.set_mode("proxy")
    b = _builder(tmp_path)
    b.with_egress(cfg)
    addr = b._ensure_egress_proxy()
    assert addr[0] == "127.0.0.1"
    assert addr[1] > 0
    b.shutdown_egress()
    assert b._egress_proxy is None


def test_bash_tool_receives_egress_mode(tmp_path):
    cfg = EgressConfig(path=tmp_path / "egress.json")
    cfg.set_mode("off")
    b = AgentBuilder(str(tmp_path), _Provider())
    b.with_default_tools().with_mode("default").with_egress(cfg)
    bash = next(t for t in b._get_tools() if t.name == "bash")
    assert bash._egress_mode == "off"
