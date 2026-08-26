"""Tests for BashTool."""

import platform

import pytest

from cluxmate.tools.bash import BashTool, _is_wsl_bash, _resolve_shell

IS_WIN = platform.system() == "Windows"


@pytest.mark.asyncio
async def test_bash_echo():
    tool = BashTool()
    result = await tool.execute(command="echo hello")
    assert "hello" in result


@pytest.mark.asyncio
async def test_bash_nonexistent_command():
    tool = BashTool()
    result = await tool.execute(command="nonexistent_command_xyz")
    assert "not found" in result.lower() or "exit code" in result.lower()


@pytest.mark.asyncio
async def test_bash_run_safe_error():
    tool = BashTool()
    tr = await tool.run_safe("call_1", command="nonexistent_command_xyz")
    assert tr.is_error or "exit code:" in tr.content.lower()


# ---------------------------------------------------------------------------
# WSL bash must never be chosen as the shell (it escapes the Low-IL sandbox)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not IS_WIN, reason="windows-only")
def test_is_wsl_bash_identifies_system32_launcher():
    assert _is_wsl_bash(r"C:\Windows\System32\bash.exe") is True
    assert _is_wsl_bash(r"C:\Windows\system32\BASH.EXE") is True  # case-insensitive
    assert _is_wsl_bash(r"C:\Program Files\Git\bin\bash.exe") is False
    assert _is_wsl_bash(r"C:\Program Files\Git\usr\bin\bash.exe") is False


@pytest.mark.skipif(not IS_WIN, reason="windows-only")
def test_resolve_shell_rejects_wsl_bash(monkeypatch):
    # Even when WSL bash exists AND "works", it must be rejected → cmd.exe.
    monkeypatch.setattr("cluxmate.tools.bash.shutil.which",
                        lambda _: r"C:\Windows\System32\bash.exe")
    monkeypatch.setattr("cluxmate.tools.bash._bash_works", lambda _: True)
    monkeypatch.setattr("cluxmate.tools.bash.platform.system", lambda: "Windows")
    use_shell, prefix = _resolve_shell()
    assert use_shell is True and prefix == []


@pytest.mark.skipif(not IS_WIN, reason="windows-only")
def test_resolve_shell_accepts_native_git_bash(monkeypatch):
    monkeypatch.setattr("cluxmate.tools.bash.shutil.which",
                        lambda _: r"C:\Program Files\Git\bin\bash.exe")
    monkeypatch.setattr("cluxmate.tools.bash._bash_works", lambda _: True)
    monkeypatch.setattr("cluxmate.tools.bash.platform.system", lambda: "Windows")
    use_shell, prefix = _resolve_shell()
    assert use_shell is False
    assert prefix == [r"C:\Program Files\Git\bin\bash.exe", "-c"]
