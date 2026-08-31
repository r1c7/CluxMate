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
