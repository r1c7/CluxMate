"""Tests for web_fetch tool."""

import json

import pytest

from cluxmate.tools.web_fetch import WebFetchTool


@pytest.fixture
def tool():
    return WebFetchTool()


# ── Real HTTP (httpbin.org) tests ──────────────────────────────────


@pytest.mark.network
@pytest.mark.asyncio
async def test_basic_get(tool):
    """Fetch a known static page and verify content is returned."""
    result = await tool.execute(url="https://httpbin.org/get")
    assert "Status: 200 OK" in result
    assert "httpbin" in result.lower()


@pytest.mark.network
@pytest.mark.asyncio
async def test_custom_headers(tool):
    """Custom headers should be sent and reflected by httpbin."""
    result = await tool.execute(
        url="https://httpbin.org/headers",
        headers={"X-Test": "hello"},
        format="verbose",
    )
    data = json.loads(result)
    assert data["status"] == 200
    body = json.loads(data["body"])
    assert body["headers"]["X-Test"] == "hello"


@pytest.mark.network
@pytest.mark.asyncio
async def test_post_with_body(tool):
    """POST with body should echo back correctly."""
    result = await tool.execute(
        url="https://httpbin.org/post",
        method="POST",
        body='{"key": "value"}',
        headers={"Content-Type": "application/json"},
        format="verbose",
    )
    data = json.loads(result)
    assert data["status"] == 200
    body = json.loads(data["body"])
    assert body["json"] == {"key": "value"}


@pytest.mark.network
@pytest.mark.asyncio
async def test_raw_format_prefix(tool):
    """Raw format should have Status: and Content-Type: prefix lines."""
    result = await tool.execute(url="https://httpbin.org/get")
    lines = result.split("\n")
    assert lines[0].startswith("Status:")
    assert lines[1].startswith("Content-Type:")
    assert lines[2] == ""


@pytest.mark.network
@pytest.mark.asyncio
async def test_verbose_format(tool):
    """Verbose format should return valid JSON with filtered headers."""
    result = await tool.execute(url="https://httpbin.org/get", format="verbose")
    data = json.loads(result)
    assert "status" in data
    assert "status_text" in data
    assert "headers" in data
    assert "body" in data
    assert "redirect_url" in data
    for key in data["headers"]:
        assert key in ("content-type", "content-length", "last-modified")


@pytest.mark.network
@pytest.mark.asyncio
async def test_verbose_include_all_headers(tool):
    """include_all_headers=True should return all headers."""
    result = await tool.execute(
        url="https://httpbin.org/get",
        format="verbose",
        include_all_headers=True,
    )
    data = json.loads(result)
    assert len(data["headers"]) >= 3


@pytest.mark.asyncio
async def test_nonexistent_domain(tool):
    """Non-existent domain should return error gracefully."""
    result = await tool.execute(url="https://nonexistent-domain-xyz123.com")
    assert "error" in result.lower() or "Error" in result


@pytest.mark.network
@pytest.mark.asyncio
async def test_not_found_status(tool):
    """404 should return content with status line showing it."""
    result = await tool.execute(url="https://httpbin.org/status/404")
    assert "Status: 404" in result


@pytest.mark.asyncio
async def test_plan_mode_rejects_write_methods(tool):
    """In plan mode, POST/PUT/PATCH/DELETE should be rejected."""
    tool.plan_mode = True
    result = await tool.execute(url="https://httpbin.org/post", method="POST")
    assert "error" in result.lower() or "read-only" in result.lower()


@pytest.mark.network
@pytest.mark.asyncio
async def test_plan_mode_allows_get(tool):
    """GET should work normally in plan mode."""
    tool.plan_mode = True
    result = await tool.execute(url="https://httpbin.org/get")
    assert "Status: 200 OK" in result


def test_method_enum_in_schema(tool):
    """Schema should include all methods regardless of plan_mode."""
    schema = tool.input_schema
    method_prop = schema["properties"]["method"]
    assert set(method_prop["enum"]) == {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"}


# ── Unit tests (no network) ────────────────────────────────────────


def test_input_schema_defaults(tool):
    """Verify default values in schema properties."""
    schema = tool.input_schema
    assert schema["properties"]["method"]["default"] == "GET"
    assert schema["properties"]["format"]["default"] == "raw"
    assert schema["properties"]["timeout_ms"]["default"] == 30000
    assert schema["properties"]["timeout_ms"]["minimum"] == 1000
    assert schema["properties"]["timeout_ms"]["maximum"] == 120000
    assert schema["properties"]["include_all_headers"]["default"] is False


def test_risk_level_safe(tool):
    """risk_level should be safe (read-only)."""
    assert tool.risk_level == "safe"


def test_plan_mode_default_false(tool):
    """plan_mode should default to False."""
    assert tool.plan_mode is False


def test_plan_mode_construction():
    """WebFetchTool can be constructed with plan_mode=True."""
    t = WebFetchTool(plan_mode=True)
    assert t.plan_mode is True


@pytest.mark.asyncio
async def test_plan_mode_write_methods_list(tool):
    """All non-read-only methods should be rejected in plan mode."""
    tool.plan_mode = True
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        result = await tool.execute(url="https://example.com", method=method)
        assert "error" in result.lower()


@pytest.mark.asyncio
async def test_tool_name_and_description(tool):
    """Verify tool identity."""
    assert tool.name == "web_fetch"
    assert "GET" in tool.description
    assert "POST" in tool.description


# ── SSRF guard (no network: loopback literals + local HTTP server) ────

import threading
import http.server
from cluxmate.core.ssrf_config import SsrConfig


class _QuietHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"hello from ssrf-test"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class _RedirectHandler(http.server.BaseHTTPRequestHandler):
    """302 to another 127.0.0.1 port (which is NOT allow-listed)."""
    target = ""

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", self.target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def ssrf_tool(tmp_path):
    def _make(allow):
        cfg = SsrConfig(path=tmp_path / "ssrf.json")
        cfg.set_rules(allow=allow, block_extra=[])
        return WebFetchTool(ssrf=cfg)
    return _make


def _serve(handler_cls, **kwargs):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    if kwargs:
        for k, v in kwargs.items():
            setattr(handler_cls, k, v)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


@pytest.mark.asyncio
async def test_ssrf_blocks_internal_without_allow(ssrf_tool):
    # 127.0.0.1 literal, no allow → blocked BEFORE any connection attempt.
    tool = ssrf_tool(allow=[])
    result = await tool.execute(url="http://127.0.0.1:1/")
    assert "SSRF guard blocked" in result


@pytest.mark.asyncio
async def test_ssrf_allows_allowlisted_loopback(ssrf_tool):
    srv = _serve(_QuietHandler)
    try:
        tool = ssrf_tool(allow=[f"127.0.0.1:{srv.server_port}"])
        result = await tool.execute(url=f"http://127.0.0.1:{srv.server_port}/")
        assert "Status: 200 OK" in result
        assert "hello from ssrf-test" in result
    finally:
        srv.shutdown()


@pytest.mark.asyncio
async def test_ssrf_blocks_redirect_hop(ssrf_tool):
    # First hop allowed, redirect target NOT allowed → the hook must catch the
    # second hop before connecting to it.
    redirector = _serve(_RedirectHandler)
    try:
        tool = ssrf_tool(allow=[f"127.0.0.1:{redirector.server_port}"])
        # Redirect to the same host on a different (not allowed) port.
        _RedirectHandler.target = f"http://127.0.0.1:{redirector.server_port + 1}/"
        result = await tool.execute(url=f"http://127.0.0.1:{redirector.server_port}/")
        assert "SSRF guard blocked" in result
    finally:
        redirector.shutdown()
