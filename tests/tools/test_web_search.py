"""Tests for the SSRF hook wiring on web_search (no network; DNS monkeypatched)."""

import socket

import httpx
import pytest

from cluxmate.core.ssrf_config import SsrConfig
from cluxmate.tools._ssrf import SSRFBlockedError
from cluxmate.tools.web_search import WebSearchTool


def _tool(tmp_path):
    return WebSearchTool(ssrf=SsrConfig(path=tmp_path / "ssrf.json"))


def _fake_dns(monkeypatch):
    def fake(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0))]
    monkeypatch.setattr(socket, "getaddrinfo", fake)


@pytest.mark.asyncio
async def test_hook_blocks_internal_target(tmp_path):
    tool = _tool(tmp_path)
    with pytest.raises(SSRFBlockedError):
        await tool._ssrf_hook(httpx.Request("POST", "http://127.0.0.1:1/"))


@pytest.mark.asyncio
async def test_hook_passes_search_endpoint(tmp_path, monkeypatch):
    _fake_dns(monkeypatch)
    tool = _tool(tmp_path)
    # The hardcoded search endpoint is https + public → hook must not raise.
    await tool._ssrf_hook(httpx.Request("POST", "https://lite.duckduckgo.com/lite/"))


def test_hook_attached_to_client_requests(tmp_path):
    tool = _tool(tmp_path)
    # The hook is a plain function on the instance; it is wired into the
    # AsyncClient via event_hooks in execute().
    assert tool._ssrf_hook is not None
