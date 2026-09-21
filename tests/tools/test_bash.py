"""Tests for BashTool."""

import asyncio
import platform
import sys
import time

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


def test_assess_command_risk_tiers():
    tool = BashTool()
    # critical: device/system-level destruction — never auto-approved.
    assert tool.assess_command_risk("format C:") == "critical"
    assert tool.assess_command_risk("mkfs.ext4 /dev/sda1") == "critical"
    assert tool.assess_command_risk("dd if=/dev/zero of=/dev/sda") == "critical"
    assert tool.assess_command_risk("echo x > /dev/sda") == "critical"
    assert tool.assess_command_risk("chmod 777 /etc/passwd") == "critical"
    # dangerous: workspace-bounded destruction — always-allowable.
    assert tool.assess_command_risk("rm -rf build") == "dangerous"
    assert tool.assess_command_risk("del file.txt") == "dangerous"
    assert tool.assess_command_risk("rmdir foo") == "dangerous"
    assert tool.assess_command_risk("git reset --hard HEAD~1") == "dangerous"
    assert tool.assess_command_risk("git push --force origin main") == "dangerous"
    # write.
    assert tool.assess_command_risk("npm install") == "write"
    assert tool.assess_command_risk("git commit -m x") == "write"
    # safe.
    assert tool.assess_command_risk("ls -la") == "safe"
    assert tool.assess_command_risk("git status") == "safe"


def test_classify_command_categories():
    tool = BashTool()
    cc = tool.classify("rm -rf build")
    assert cc.level == "dangerous" and cc.categories == frozenset({"rm"})
    cc = tool.classify("git reset --hard HEAD~1")
    assert cc.level == "dangerous" and cc.categories == frozenset({"git-reset-hard"})
    cc = tool.classify("git push --force origin main")
    assert cc.categories == frozenset({"git-push-force"})
    # A command matching multiple destructive categories reports all of them.
    cc = tool.classify("rm x && git reset --hard")
    assert cc.level == "dangerous" and cc.categories == frozenset({"rm", "git-reset-hard"})
    # critical + write/safe have no authorizable categories.
    assert tool.classify("format C:").categories == frozenset()
    assert tool.classify("npm install").categories == frozenset()
    assert tool.classify("git status").categories == frozenset()


def test_classify_code_runners():
    tool = BashTool()
    # Interpreters → dangerous with the interpreter as the category.
    for cmd, cat in [
        ("python script.py", "python"),
        ("python3.11 -m pip install x", "python"),
        ("sudo python foo.py", "python"),
        ("node app.js", "node"),
        ("ruby x.rb", "ruby"),
        ("bash deploy.sh", "shell"),
        ("sh -c 'echo hi'", "shell"),
        ("powershell script.ps1", "powershell"),
    ]:
        cc = tool.classify(cmd)
        assert cc.level == "dangerous", cmd
        assert cc.categories == frozenset({cat}), (cmd, cc.categories)
    # Build/run tools → dangerous.
    assert tool.classify("go run .").categories == frozenset({"go"})
    assert tool.classify("./gradlew test").categories == frozenset({"gradle"})
    assert tool.classify("make all").categories == frozenset({"make"})
    # npm-family script runners → dangerous "npm"; install stays write.
    assert tool.classify("npm run build").categories == frozenset({"npm"})
    assert tool.classify("npm test").categories == frozenset({"npm"})
    assert tool.classify("npx jest").categories == frozenset({"npm"})
    assert tool.classify("yarn run dev").categories == frozenset({"npm"})
    assert tool.classify("npm install").level == "write"
    assert tool.classify("pip install requests").level == "write"
    # No preset entry → fallback category "run" (script / binary path).
    assert tool.classify("./myscript.sh").categories == frozenset({"run"})
    assert tool.classify("/usr/local/bin/mytool").categories == frozenset({"run"})
    assert tool.classify("deploy.sh").categories == frozenset({"run"})
    # Inline destructive marker still wins over the runner category.
    cc = tool.classify('python -c "os.system(\'rm -rf /tmp/x\')"')
    assert cc.categories == frozenset({"rm"})
    # A runner buried in a compound command is still classified dangerous.
    assert tool.classify("git commit && python x.py").categories == frozenset({"python"})
    assert tool.classify("python a.py && node b.js").categories == frozenset({"python", "node"})
    # A safe echo containing the word "python" is NOT a runner (first-token rule).
    assert tool.classify("echo python script.py").level == "safe"
    # Known read-only commands stay safe (not misclassified as `run`).
    assert tool.classify("ls -la").level == "safe"
    assert tool.classify("git status").level == "safe"
    assert tool.classify("cat file.txt").level == "safe"


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


# ---------------------------------------------------------------------------
# A timeout must RETURN, and must kill the whole process tree
# ---------------------------------------------------------------------------

# The grandchild appends to a counter file every 50ms until killed, so the test
# can prove the timeout killed it (file stops growing) rather than only killing
# its parent. sys.executable in the helper scripts keeps this independent of
# whatever "python" happens to be on PATH.
_CHILD_SCRIPT = """
import subprocess, sys, time
subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])
time.sleep(60)
"""

_GRANDCHILD_SCRIPT = """
import pathlib, sys, time
counter = pathlib.Path(sys.argv[1])
for i in range(300):
    with counter.open("a") as fh:
        fh.write(f"{i}\\n")
    time.sleep(0.05)
"""

# Spawns the grandchild and exits at once: it still holds our stdout pipe, but
# its parent is gone, so a tree walk started at the shell can no longer reach it.
_CHILD_ESCAPE_SCRIPT = """
import subprocess, sys
subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])
"""


async def _wait_for_writes(path, *, timeout=20.0) -> None:
    """Block until the helper's grandchild has actually produced output.

    Awaits between polls on purpose: the tool's work happens on an executor
    thread that only starts once the event loop gets to run the task.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if path.stat().st_size > 0:
                return
        except OSError:
            pass
        await asyncio.sleep(0.1)
    raise AssertionError(f"the helper command never started writing {path}")


@pytest.mark.asyncio
async def test_bash_timeout_returns_and_kills_the_process_tree(tmp_path):
    """A timed-out command must come back even when a grandchild holds stdout.

    Regression (session 2c2811eccf26, 88 minutes with no result): the timeout
    path was ``subprocess.run(..., timeout=N)``, whose Windows branch kills only
    the direct child (cmd.exe) and then calls ``communicate()`` AGAIN with no
    timeout to collect output. A surviving descendant still holds the stdout
    pipe's write end, so that read never sees EOF: the TimeoutExpired — and with
    it the "[Command timed out]" message — is delayed until the descendant dies
    on its own, i.e. forever for a hung one. The same shape exists on POSIX
    (killing the shell leaves the real work alive).
    """
    child = tmp_path / "child.py"
    child.write_text(_CHILD_SCRIPT)
    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(_GRANDCHILD_SCRIPT)
    counter = tmp_path / "counter.txt"

    tool = BashTool()
    command = f'"{sys.executable}" "{child}" "{grandchild}" "{counter}"'
    started = time.monotonic()
    result = await asyncio.wait_for(
        tool.execute(command=command, timeout_ms=1500), timeout=40
    )
    elapsed = time.monotonic() - started

    assert "timed out" in result.lower()
    # Without the tree kill this returns only when the grandchild finishes
    # (TTL 15s) — and never at all for a command that hangs forever.
    assert elapsed < 8, f"timeout did not return promptly: {elapsed:.1f}s"

    time.sleep(1.0)
    first = counter.read_text()
    time.sleep(1.0)
    assert counter.read_text() == first, "a descendant survived the timeout kill"


@pytest.mark.asyncio
async def test_bash_timeout_stays_bounded_when_a_descendant_escapes_the_kill(tmp_path):
    """The bound holds even when the kill misses a survivor holding stdout.

    The escapee is no longer reachable from the shell, so the drain read cannot
    reach EOF: the tool must abandon the output and return the timeout message
    anyway. Closing our own pipe ends instead is a REGRESSION (measured) — the
    parked reader thread holds the buffered reader's lock, so ``close()`` blocks
    until the escapee dies, and the call never returns.
    """
    child = tmp_path / "escape_child.py"
    child.write_text(_CHILD_ESCAPE_SCRIPT)
    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(_GRANDCHILD_SCRIPT)
    counter = tmp_path / "counter.txt"

    tool = BashTool()
    command = f'"{sys.executable}" "{child}" "{grandchild}" "{counter}"'
    started = time.monotonic()
    result = await asyncio.wait_for(
        tool.execute(command=command, timeout_ms=1500), timeout=30
    )
    elapsed = time.monotonic() - started

    assert "timed out" in result.lower()
    # timeout + the bounded drain + slack — not the escapee's 15s lifetime, and
    # not forever (the assertion the close()-the-pipes variant fails).
    assert elapsed < 12, f"the drain was not bounded: {elapsed:.1f}s"


@pytest.mark.asyncio
async def test_bash_cancel_running_kills_the_command_tree(tmp_path):
    """A cancelled turn must stop the command it was running.

    The loop abandons the executor thread blocked on the child (the turn is
    gone), so `cancel_running()` is the only thing that can stop it — without
    it a stopped `pytest`/`npm run` keeps burning CPU and holding the
    workspace's files until the process finishes on its own.
    """
    child = tmp_path / "child.py"
    child.write_text(_CHILD_SCRIPT)
    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(_GRANDCHILD_SCRIPT)
    counter = tmp_path / "counter.txt"

    tool = BashTool()
    command = f'"{sys.executable}" "{child}" "{grandchild}" "{counter}"'
    task = asyncio.create_task(tool.execute(command=command, timeout_ms=60_000))
    await _wait_for_writes(counter)
    started = time.monotonic()
    tool.cancel_running()
    await asyncio.wait_for(task, timeout=20)
    elapsed = time.monotonic() - started

    assert elapsed < 10, f"the cancelled command still held the call: {elapsed:.1f}s"
    time.sleep(1.0)
    first = counter.read_text()
    time.sleep(1.0)
    assert counter.read_text() == first, "a descendant survived the cancellation"

