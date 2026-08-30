"""Tests for the ssrf/config JSON-RPC methods (host-side, not a model tool)."""

import pytest

from cluxmate.core.jsonrpc_server import JsonRpcServer
from cluxmate.core.ssrf_config import SsrConfig


def _server(tmp_path):
    s = JsonRpcServer()
    s._ssrf_config = SsrConfig(path=tmp_path / "ssrf.json")
    return s


def test_set_and_snapshot(tmp_path):
    s = _server(tmp_path)
    result = s._set_ssrf_config({"allow": ["localhost:3000"], "block_extra": ["10.0.0.0/8"]})
    assert result == {"allow": ["localhost:3000"], "block_extra": ["10.0.0.0/8"]}
    assert s._ssrf_snapshot() == {"allow": ["localhost:3000"], "block_extra": ["10.0.0.0/8"]}


def test_set_filters_non_strings(tmp_path):
    s = _server(tmp_path)
    result = s._set_ssrf_config({"allow": ["localhost", 123, None], "block_extra": []})
    assert result == {"allow": ["localhost"], "block_extra": []}


def test_set_invalid_entry_raises(tmp_path):
    s = _server(tmp_path)
    with pytest.raises(ValueError):
        s._set_ssrf_config({"allow": ["bad entry with spaces"], "block_extra": []})


def test_snapshot_without_store(tmp_path):
    s = JsonRpcServer()  # never initialized
    assert s._ssrf_snapshot() == {"allow": [], "block_extra": []}
