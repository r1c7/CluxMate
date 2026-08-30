"""Tests for builder wiring of the shared SsrConfig into web tools."""

from cluxmate.core.builder import AgentBuilder
from cluxmate.core.ssrf_config import SsrConfig
from cluxmate.tools.web_fetch import WebFetchTool
from cluxmate.tools.web_search import WebSearchTool


class _Provider:
    pass


def _builder(tmp_path):
    b = AgentBuilder(str(tmp_path), _Provider())
    b.with_default_tools().with_mode("default")
    return b


def test_web_tools_receive_ssrf_config(tmp_path):
    cfg = SsrConfig(path=tmp_path / "ssrf.json")
    b = _builder(tmp_path)
    b.with_ssrf(cfg)
    tools = b._get_tools()
    wf = next(t for t in tools if t.name == "web_fetch")
    ws = next(t for t in tools if t.name == "web_search")
    assert wf._ssrf is cfg
    assert ws._ssrf is cfg


def test_plan_mode_web_fetch_gets_ssrf_config(tmp_path):
    cfg = SsrConfig(path=tmp_path / "ssrf.json")
    b = AgentBuilder(str(tmp_path), _Provider())
    b.with_default_tools().with_mode("plan")
    b.with_ssrf(cfg)
    wf = next(t for t in b._get_tools() if t.name == "web_fetch")
    assert wf.plan_mode is True
    assert wf._ssrf is cfg


def test_child_builder_inherits_ssrf_config(tmp_path):
    cfg = SsrConfig(path=tmp_path / "ssrf.json")
    b = _builder(tmp_path)
    b.with_ssrf(cfg)
    child = b._child_builder("explore", "child-1")
    assert child._ssrf is cfg
