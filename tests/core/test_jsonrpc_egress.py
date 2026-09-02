"""Tests for the egress/config JSON-RPC methods (host-side, not a model tool)."""

import pytest

from cluxmate.core.egress_config import EgressConfig
from cluxmate.core.jsonrpc_server import JsonRpcServer


def _server(tmp_path):
    s = JsonRpcServer()
    s._egress_config = EgressConfig(path=tmp_path / "egress.json")
    return s


def test_set_and_snapshot(tmp_path):
    s = _server(tmp_path)
    assert s._set_egress_config({"mode": "proxy"}) == {"mode": "proxy"}
    assert s._egress_snapshot() == {"mode": "proxy"}


def test_set_invalid_mode_raises(tmp_path):
    s = _server(tmp_path)
    with pytest.raises(ValueError):
        s._set_egress_config({"mode": "bogus"})


def test_snapshot_without_store(tmp_path):
    s = JsonRpcServer()  # never initialized
    assert s._egress_snapshot() == {"mode": "shared"}
