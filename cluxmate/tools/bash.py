"""Bash tool — execute shell commands."""

import asyncio
import locale
import os
import platform
import re
import shutil
import subprocess
from typing import Any

from .base import BaseTool
from ._sandbox import ESCALATION_SCHEMA_FIELDS, ShellSandbox

# Strip ANSI escape sequences (colors, cursor movements, etc.) from output
# so the desktop renderer doesn't show gibberish like \x1b[32m.
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\][0-9]+;[^\x07]*\x07|\x1b\][0-9]+;[^\x1b]*\x1b\\\\')

# Access/permission-denied fingerprints a sandboxed child reports when it tries
# to write outside its allowed roots: NO_WRITE_UP on Windows ("拒绝访问" /
# "Access is denied"), read-only bind / namespaced EPERM on Linux ("Read-only
# file system" / "Permission denied" / "Operation not permitted"). Matched
# against the DECODED output (so the GBK fallback in _decode applies first).
_ACCESS_DENIED_RE = re.compile(
    r"access\s+is\s+denied|access\s+denied|permission\s+denied|"
    r"read-?only\s+file\s+system|operation\s+not\s+permitted|"
    r"\u62d2\u7edd\u8bbf\u95ee",  # 拒绝访问
    re.IGNORECASE,
)

# Surfaced after a failed sandboxed command whose output smells like the
# sandbox boundary. Honest: it can't be sure the denial is the sandbox (e.g. a
# remote "permission denied (publickey)" is unrelated), so it says "may be" —
# but when it IS the boundary, it gives the model the ONE structured next step
# (escalate → approval) instead of inviting it to try endless variants.
_SANDBOX_DENIAL_HINT = (
    "[sandbox: access denied]\n"
    "This command failed with a permission/access error. This MAY be the sandbox "
    "boundary: a sandboxed shell can only write the workspace, its granted "
    "folders, and its temp dir — writes elsewhere (or deleting medium-integrity "
    "files on Windows) are blocked by the OS. If that is what this command was "
    "doing, retry it ONCE with sandbox_permissions=\"danger-full-access\" and a "
    "one-sentence justification; the user is then asked to approve. Do not try "
    "other deletion/write variants — they will be blocked the same way. If it "
    "was an unrelated permission error (e.g. a remote auth failure), ignore this "
    "note."
)


def _bash_works(bash_path: str) -> bool:
    """Test-run bash to ensure it actually works (WSL bash.exe fails if no distro)."""
    try:
        result = subprocess.run(
            [bash_path, "-c", "exit 0"],
            capture_output=True, timeout=5,
            # stdin MUST be detached. Under `agent stdio` the server's stdin is
            # the JSON-RPC pipe; if bash inherits it, Git Bash's bash.exe blocks
            # on it at startup and hangs the full 5s timeout on every launch —
            # then falls back to cmd.exe. See _detect_shell.
            stdin=subprocess.DEVNULL,
        )
        return result.returncode == 0
    except Exception:
        return False


def _is_wsl_bash(bash_path: str) -> bool:
    """Is this the WSL launcher (C:\\Windows\\System32\\bash.exe)?

    WSL's bash spawns a Linux process whose file access goes through DrvFS
    and WSL-interop, both of which run OUTSIDE the Windows integrity-level
    (Low-IL) sandbox — a sandboxed command routed through it would escape the
    NO_WRITE_UP boundary entirely. Native Win32 bashes (Git Bash / MSYS2) live
    elsewhere and stay inside the IL sandbox, so only the System32 launcher is
    rejected.
    """
    windir = os.environ.get("WINDIR", r"C:\Windows")
    wsl_dir = os.path.normcase(os.path.join(windir, "System32"))
    return os.path.normcase(os.path.dirname(os.path.abspath(bash_path))) == wsl_dir


def _resolve_shell() -> tuple[bool, list[str]]:
    """Return (use_shell, prefix_args).

    On Windows, prefer a real bash if found on PATH (Git Bash, etc.) so the
    system prompt's bash syntax actually runs under bash, not cmd.exe.
    Validates the bash works before trusting it (WSL's bash.exe fails if no
    Linux distro is installed). WSL's System32 bash.exe is ALWAYS rejected: it
    escapes the Low-IL sandbox (Linux-side DrvFS/interop bypass the Windows
    integrity boundary). Returns (True, []) to fall back to shell=True
    (cmd.exe on Windows) when no working native bash is available.
    """
    if platform.system() == "Windows":
        bash = shutil.which("bash")
        if bash and _bash_works(bash) and not _is_wsl_bash(bash):
            return False, [bash, "-c"]
        return True, []
    return True, []


def _subprocess_env() -> dict[str, str]:
    """Return environment dict that forces UTF-8 output from subprocesses."""
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["LANG"] = os.environ.get("LANG", "C.UTF-8")
    env["LC_ALL"] = os.environ.get("LC_ALL", "C.UTF-8")
    return env


def _decode(data: bytes) -> str:
    """Decode bytes to string, trying UTF-8 first then system locale (GBK etc).

    ANSI escape sequences and leading chcp banner lines are stripped from the
    decoded result. On Chinese Windows, cmd.exe outputs GBK (cp936) which won't
    decode as valid UTF-8, so the system-locale fallback handles it correctly."""
    for encoding in ("utf-8", locale.getpreferredencoding(False)):
        try:
            text = data.decode(encoding)
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            return _ANSI_RE.sub("", text)
        except (UnicodeDecodeError, LookupError):
            continue
    text = data.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _ANSI_RE.sub("", text)


class BashTool(BaseTool):
    """Execute a shell command and return its output."""

    def __init__(
        self,
        timeout_ms: int = 120_000,
        workdir: str | None = None,
        sandbox: ShellSandbox | None = None,
        sandbox_required: bool = False,
    ):
        self._timeout_ms = timeout_ms
        self._workdir = workdir
        # Sandbox wiring (phase 1): sandbox is the backend to run under;
        # sandbox_required=True + sandbox=None is the FAIL-CLOSED state —
        # no backend available, so commands are refused, never run bare.
        self._sandbox = sandbox
        self._sandbox_required = sandbox_required

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return (
            "Execute a shell command in the project directory and return "
            "its stdout and stderr. Use for running tests, building, "
            "installing packages, file operations, and git commands."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to run.",
                },
                "timeout_ms": {
                    "type": "integer",
                    "description": "Optional timeout in milliseconds.",
                },
                **ESCALATION_SCHEMA_FIELDS,
            },
            "required": ["command"],
        }

    @property
    def risk_level(self) -> str:
        return "safe"

    def assess_command_risk(self, command: str) -> str:
        _destructive = [
            r'(?<!git )\brm\b', r'\bgit\s+push\s+.*--force', r'\bgit\s+reset\s+--hard',
            r'\bdel\b', r'\brmdir\b', r'\bformat\b', r'\bchmod\s+777\b',
            r'>\s*/dev/', r'\bdd\b', r'\bmkfs\b',
        ]
        _write = [
            r'\b(git\s+commit|git\s+push|git\s+add)\b',
            r'\b(npm|pip|yarn|pnpm)\s+install\b',
            r'\bmkdir\b', r'\btouch\b', r'\bmv\b', r'\bcp\b',
            r'\bwget\b', r'\bcurl\b.*\s(--output|-o)\b',
            r'(?<![12])>>?\s*\S(?!&)',
        ]
        for pat in _destructive:
            if re.search(pat, command):
                return "dangerous"
        for pat in _write:
            if re.search(pat, command):
                return "write"
        return "safe"

    async def execute(
        self,
        command: str,
        timeout_ms: int | None = None,
        sandbox_permissions: str | None = None,
        justification: str | None = None,
    ) -> str:
        timeout = (timeout_ms or self._timeout_ms) / 1000.0
        cwd = self._workdir or os.getcwd()
        use_shell, prefix = _resolve_shell()
        env = _subprocess_env()

        if use_shell and platform.system() == "Windows":
            # chcp 65001 switches cmd.exe to UTF-8 (code page 65001) so
            # Chinese characters in command output are UTF-8, not GBK.
            command = f"chcp 65001 >NUL && {command}"

        # Sandbox dispatch (phase 1): when a backend is configured, run the
        # command under it. When sandboxing is REQUIRED but no backend is
        # available, FAIL CLOSED — refuse rather than run bare. A
        # danger-full-access escalation bypasses the sandbox entirely (the
        # approval already happened in on_tool_start): it runs bare.
        sandboxed = self._sandbox is not None or self._sandbox_required
        if sandboxed and sandbox_permissions != "danger-full-access":
            return await self._run_sandboxed(command, use_shell, prefix,
                                             cwd, env, timeout)

        def _run():
            try:
                # stdin=DEVNULL is critical: without it the child inherits the
                # JSON-RPC server's stdin (a pipe that only ever carries protocol
                # lines). Any interactive prompt — cmd.exe's "Overwrite? (Y/N)"
                # on move/copy, "Are you sure (Y/N)?" on del/rmdir — would then
                # block forever waiting on input that never comes, until the
                # 180s tool timeout kills it. Feeding EOF makes such commands
                # fail fast instead of hanging.
                if use_shell:
                    result = subprocess.run(
                        command, shell=True, capture_output=True,
                        stdin=subprocess.DEVNULL,
                        timeout=timeout, cwd=cwd, env=env,
                    )
                else:
                    result = subprocess.run(
                        prefix + [command], capture_output=True,
                        stdin=subprocess.DEVNULL,
                        timeout=timeout, cwd=cwd, env=env,
                    )
                output = _decode(result.stdout)
                if result.stderr:
                    output += "\n" + _decode(result.stderr)
                if result.returncode != 0:
                    output += f"\n[exit code: {result.returncode}]"
                return output.strip() or "(no output)"
            except subprocess.TimeoutExpired:
                return f"[Command timed out after {timeout}s]\n{command}"
            except FileNotFoundError:
                return f"Command not found: {command}"
            except Exception as e:
                return f"Error executing command: {e}"

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _run)

    async def _run_sandboxed(
        self,
        command: str,
        use_shell: bool,
        prefix: list[str],
        cwd: str,
        env: dict[str, str],
        timeout: float,
    ) -> str:
        """Run the command under the configured sandbox backend (or fail closed)."""
        sb = self._sandbox
        if sb is None:
            # sandbox_required but no backend: REFUSE. Never fall back to a
            # bare subprocess (docs/plans/sandbox-threat-model.md fail-closed).
            return (
                "Error: command refused — shell sandboxing is enabled on this "
                "session but no sandbox backend is available on this platform. "
                "Install one (Linux: bubblewrap; Windows: icacls/ctypes) or "
                "set CLUXMATE_BASH_SANDBOX=off to explicitly disable sandboxing."
            )

        def _run():
            try:
                if use_shell:
                    result = sb.run(
                        argv=[], shell_cmd=command, cwd=cwd,
                        timeout=timeout, env=env,
                    )
                else:
                    # bash -c "<command>": pass through the resolved shell so
                    # the backend invokes it inside the sandbox.
                    result = sb.run(
                        argv=prefix + [command], shell_cmd=None, cwd=cwd,
                        timeout=timeout, env=env,
                    )
                output = _decode(result.stdout)
                if result.stderr:
                    output += "\n" + _decode(result.stderr)
                if result.returncode != 0:
                    output += f"\n[exit code: {result.returncode}]"
                    # A sandboxed child that hit NO_WRITE_UP / a read-only bind
                    # fails with an access-denied stderr; surface a hint instead
                    # of a bare failure the model would blindly retry.
                    if _ACCESS_DENIED_RE.search(output):
                        output += "\n" + _SANDBOX_DENIAL_HINT
                return output.strip() or "(no output)"
            except subprocess.TimeoutExpired:
                return f"[Command timed out after {timeout}s]\n{command}"
            except FileNotFoundError as e:
                return f"Command not found: {e}"
            except Exception as e:
                return f"Error executing command (sandbox {sb.name}): {e}"

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _run)
