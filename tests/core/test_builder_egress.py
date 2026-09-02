"""Tests for builder wiring of EgressConfig + the proxy lifecycle."""

from cluxmate.core.builder import AgentBuilder
from cluxmate.core.egress_config import EgressConfig
from cluxmate.core.ssrf_config import SsrConfig


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


def test_bash_tool_receives_egress_mode(tmp_path, monkeypatch):
    # Force the shell-sandbox boundary to be considered active, so this test
    # is deterministic regardless of the host's CLUXMATE_BASH_SANDBOX env.
    monkeypatch.setattr("cluxmate.core.builder.sandbox_disabled_by_env", lambda: False)
    cfg = EgressConfig(path=tmp_path / "egress.json")
    cfg.set_mode("off")
    b = AgentBuilder(str(tmp_path), _Provider())
    b.with_default_tools().with_mode("default").with_egress(cfg)
    bash = next(t for t in b._get_tools() if t.name == "bash")
    assert bash._egress_mode == "off"


def test_ensure_egress_proxy_restarts_on_allowlist_change(tmp_path):
    ssrf = SsrConfig(path=tmp_path / "ssrf.json")
    ssrf.set_rules(allow=["127.0.0.1:1"], block_extra=[])
    cfg = EgressConfig(path=tmp_path / "egress.json")
    cfg.set_mode("proxy")
    b = _builder(tmp_path)
    b.with_ssrf(ssrf).with_egress(cfg)
    addr1 = b._ensure_egress_proxy()
    ssrf.set_rules(allow=["127.0.0.1:2"], block_extra=[])
    addr2 = b._ensure_egress_proxy()
    b.shutdown_egress()
    assert addr1[1] != addr2[1]


def test_child_builder_inherits_egress_proxy(tmp_path):
    cfg = EgressConfig(path=tmp_path / "egress.json")
    cfg.set_mode("proxy")
    b = _builder(tmp_path)
    b.with_egress(cfg)
    b._ensure_egress_proxy()
    child = b._child_builder("explore", "child-1")
    assert child._egress_proxy is b._egress_proxy
    assert child._egress_proxy_allow == b._egress_proxy_allow
    b.shutdown_egress()


def test_yolo_mode_bash_gets_shared_egress(tmp_path):
    cfg = EgressConfig(path=tmp_path / "egress.json")
    cfg.set_mode("off")
    b = AgentBuilder(str(tmp_path), _Provider())
    b.with_default_tools().with_mode("yolo").with_egress(cfg)
    bash = next(t for t in b._get_tools() if t.name == "bash")
    assert bash._egress_mode == "shared"
    assert b._egress_proxy is None
