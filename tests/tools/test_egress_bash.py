"""Tests for BashTool egress-mode wiring and the Windows off fail-closed."""

import asyncio

from cluxmate.tools.bash import BashTool


def test_bash_default_egress_mode_is_shared():
    assert BashTool(workdir=".")._egress_mode == "shared"


def test_bash_off_on_windows_refuses(monkeypatch):
    monkeypatch.setattr("cluxmate.tools.bash.platform.system", lambda: "Windows")
    tool = BashTool(workdir=".", egress_mode="off")
    result = asyncio.run(tool.execute(command="echo hi"))
    assert "not supported" in result
    assert "Windows" in result
