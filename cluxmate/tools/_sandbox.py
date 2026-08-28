"""Shell sandbox runners — Phase 1 of docs/plans/sandbox-threat-model.md.

A T2 boundary for bash: the shell command runs inside an OS-level sandbox
primitive so that even a prompt-injected model cannot make an *approved*
command reach outside the workspace (file writes, permission config, etc.).

Nothing here is built from scratch — these are glue layers over OS
primitives (the threat-model doc's "three positions: build glue"):

- Linux:  bubblewrap (bwrap) mount namespaces. Root filesystem read-only,
  workspace + temp dir bind-mounted writable, minimal /dev, fresh /proc.
  Network stays SHARED (documented omission).
- macOS:  Seatbelt via ``sandbox-exec``. Everything stays at its default
  (reads, process exec, network) EXCEPT writes, which are denied everywhere
  but the workspace, temp dir, and granted folders — ``<cwd>/.cluxmate`` is
  re-denied so the sandboxed shell can't edit the agent's permission config.
  Reads/network remain unrestricted (same honest omission as the others).
- Windows: Low integrity-level (IL) token via token duplication. The
  workspace tree is labeled low-IL (mandatory label with inheritance);
  NO_WRITE_UP then blocks the low-IL child from writing anything medium-IL
  (home, ~/.cluxmate, Program Files). ``<cwd>/.cluxmate`` is deliberately
  RE-LABELED medium so the sandboxed shell cannot edit the agent's own
  permission config — mirroring the WriteFence deny subtree.
  ENFORCEMENT IS PARTIAL: a low-IL process can still READ everything and
  reach the network. Honest labeling, per the threat-model checklist.

Fail-closed: when the sandbox is enabled and no backend is available on
this platform, BashTool REFUSES to run the command (never falls back to a
bare subprocess). Escape hatch: ``CLUXMATE_BASH_SANDBOX=off`` (explicit,
env-scoped, for throwaway containers).

Mode wiring: enabled unless mode == "yolo" (same rule as WriteFence);
``chat/set_mode`` rebuilds the agent so a switch re-arms/disarms.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

# Escape hatch: explicitly disable the bash sandbox (e.g. running inside a
# disposable container where bwrap/low-IL add nothing).
ENV_DISABLE = "CLUXMATE_BASH_SANDBOX"

# ── sandbox escalation vocabulary ──────────────────────────────────────────
# The closed "wider mode" ladder: CluxMate has no runtime read-only sandbox
# (plan mode withholds write tools instead), so the only wider mode is
# danger-full-access. A call may request it via the `sandbox_permissions`
# argument (paired with a one-sentence `justification`); the agent layer
# raises the call's risk to `dangerous` so the existing approval prompt fires
# BEFORE execution, and an approval grants the wider mode for that ONE call.

ESCALATION_TARGETS = ("danger-full-access",)

# Model-facing hint appended to a sandbox denial when the operation could be
# retried with elevated access (i.e. NOT for deny-subtree rejections, which
# escalation does not open).
ESCALATION_HINT = (
    '[sandbox: escalation available — retry this exact operation once with '
    'sandbox_permissions="danger-full-access" and a one-sentence justification; '
    'the approval prompt asks the user]'
)

# Schema fragment merged into every mutating tool's parameters. The enum is
# closed; `justification` travels with it (an approval without a reason is a
# malformed ask).
ESCALATION_SCHEMA_FIELDS: dict[str, dict[str, Any]] = {
    "sandbox_permissions": {
        "type": "string",
        "enum": ["danger-full-access"],
        "description": (
            "Optional. Request elevated sandbox access for THIS call only. "
            "Set to \"danger-full-access\" to write outside the workspace "
            "(file tools skip the path fence; bash runs unsandboxed). The user "
            "is asked to approve; a rejection is final for this call. Only use "
            "after a normal attempt was denied by the sandbox, never speculatively."
        ),
    },
    "justification": {
        "type": "string",
        "description": (
            "Required together with sandbox_permissions: one sentence explaining "
            "why this exact operation needs the wider access."
        ),
    },
}


def validate_escalation_args(sandbox_permissions: Any, justification: Any) -> str | None:
    """Return an error string when the escalation args are malformed, else None.

    The pairing a tool schema can't express: the two args travel together, the
    mode must be a legal wider target, and the justification must be a
    non-empty sentence. Checked at EXECUTION in the agent loop (never trusted
    from the model).
    """
    if sandbox_permissions is None and justification is None:
        return None  # no escalation requested
    if sandbox_permissions is None or justification is None:
        return (
            "[Error: sandbox_permissions and justification must be provided "
            "together when requesting sandbox escalation.]"
        )
    if sandbox_permissions not in ESCALATION_TARGETS:
        return (
            f"[Error: sandbox_permissions must be one of "
            f"{list(ESCALATION_TARGETS)}, got {sandbox_permissions!r}.]"
        )
    if not str(justification).strip():
        return "[Error: justification must be a non-empty sentence.]"
    return None


class SandboxUnavailable(Exception):
    """No sandbox backend is available on this platform (fail-closed signal)."""


class SandboxResult:
    """subprocess.CompletedProcess-shaped result from a sandboxed run."""

    def __init__(self, returncode: int, stdout: bytes, stderr: bytes):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = bytes(stderr)


class ShellSandbox:
    """Base class: one platform backend. Subclasses implement run()."""

    #: short backend name surfaced in errors/UI
    name: str = "abstract"
    #: honest enforcement completeness (threat-model checklist item 7)
    enforcement: str = "partial"

    @classmethod
    def available(cls) -> bool:
        """Whether this backend can run on the current platform. Probing must
        be side-effect free (no labeling, no writes) — setup happens in run."""
        return False

    def run(
        self,
        argv: list[str],
        *,
        shell_cmd: str | None,
        cwd: str,
        timeout: float,
        env: dict[str, str],
    ) -> SandboxResult:
        """Execute and return a CompletedProcess-like result.

        Exactly one of ``argv`` (direct invocation, no shell=True) or
        ``shell_cmd`` (a string for the platform shell) is set, mirroring
        BashTool's dual execution paths.
        """
        raise NotImplementedError

    def spawn_popen(self, argv: list[str], *, cwd: str, env: dict[str, str]):
        """Spawn a LONG-RUNNING sandboxed child with PIPE stdio (MCP servers).

        Returns a subprocess.Popen (or Popen-shaped object). Only backends
        that support long-lived children implement this.
        """
        raise NotImplementedError(f"{self.name} does not support long-lived spawn")


# ---------------------------------------------------------------------------
# Linux: bubblewrap
# ---------------------------------------------------------------------------

class BwrapSandbox(ShellSandbox):
    """bubblewrap mount-namespace sandbox (the reference implementation).

    Layout (mount order matters — later mounts stack over earlier ones, and a
    parent mount shadowing an earlier child mount would hide it, so parents
    come first and the deny subtree last):
      --ro-bind / /                everything visible, read-only
      --bind <tmp> <tmp>           platform temp dir writable FIRST: a
                                   workspace under the temp dir (tests use
                                   mkdtemp) must not be shadowed by the temp
                                   bind that follows it
      --bind <ws> <ws>             workspace writable at its real path
      [--bind <grant> <grant> …]   user-granted folders
      --ro-bind <ws>/.cluxmate …   deny subtree re-mounted read-only LAST, so
                                   it overrides every writable bind above and
                                   the sandboxed shell can't reach the agent's
                                   own permission/config state — mirrors
                                   WriteFence.denyroots, Windows' medium
                                   re-label, Seatbelt's STATE
      --dev /dev                   minimal device nodes (null, zero, ...)
      --proc /proc                 fresh procfs
      --die-with-parent --new-session  no orphaned descendants

    The deny-subtree ``--ro-bind`` is added only when ``<ws>/.cluxmate``
    exists (``run``/``spawn_popen`` create it best-effort first); on a
    read-only workspace it is skipped, but there the workspace itself is
    already read-only so nothing can write the subtree anyway.

    NOT isolated: network (curl/npm/pip must work). Honest omission.
    """

    name = "bwrap"
    enforcement = "same-kernel"

    def __init__(self, grant_paths: list[str] | None = None):
        self._grant_paths = grant_paths or []

    @classmethod
    def available(cls) -> bool:
        return platform.system() == "Linux" and shutil.which("bwrap") is not None

    def _state_dir(self, cwd: str) -> Path:
        """The deny subtree: ``<cwd>/.cluxmate`` (resolved)."""
        return (Path(cwd).resolve() / ".cluxmate").resolve()

    def _ensure_state_dir(self, cwd: str) -> None:
        """Create ``<cwd>/.cluxmate`` so ``--ro-bind`` has a source to bind.

        Best-effort (side-effectful by design, run only from ``run`` /
        ``spawn_popen`` — never ``available``). On a read-only workspace the
        mkdir fails and ``_bwrap_argv`` omits the deny bind; that is safe
        because the workspace is read-only there anyway.
        """
        try:
            self._state_dir(cwd).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def _bwrap_argv(self, cwd: str) -> list[str]:
        tmp = Path(tempfile.gettempdir()).resolve()
        ws = Path(cwd).resolve()
        state = self._state_dir(cwd)
        argv = [
            "bwrap",
            "--ro-bind", "/", "/",
            "--bind", str(tmp), str(tmp),
            "--bind", str(ws), str(ws),
        ]
        for g in self._grant_paths:
            gp = str(Path(g).resolve())
            argv += ["--bind", gp, gp]
        if state.is_dir():
            argv += ["--ro-bind", str(state), str(state)]
        argv += [
            "--dev", "/dev",
            "--proc", "/proc",
            "--die-with-parent",
            "--new-session",
        ]
        return argv

    def run(
        self,
        argv: list[str],
        *,
        shell_cmd: str | None,
        cwd: str,
        timeout: float,
        env: dict[str, str],
    ) -> SandboxResult:
        resolved = str(Path(cwd).resolve())
        self._ensure_state_dir(resolved)
        prefix = self._bwrap_argv(resolved)
        if shell_cmd is not None:
            full = prefix + ["--", "/bin/sh", "-c", shell_cmd]
        else:
            full = prefix + ["--"] + argv
        proc = subprocess.run(
            full, capture_output=True, stdin=subprocess.DEVNULL,
            timeout=timeout, cwd=cwd, env=env,
        )
        return SandboxResult(proc.returncode, proc.stdout, proc.stderr)

    def spawn_popen(self, argv: list[str], *, cwd: str, env: dict[str, str]):
        resolved = str(Path(cwd).resolve())
        self._ensure_state_dir(resolved)
        prefix = self._bwrap_argv(resolved)
        return subprocess.Popen(
            prefix + ["--"] + argv,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=1, env=env, cwd=cwd,
            text=True, encoding="utf-8", errors="replace",
        )


# ---------------------------------------------------------------------------
# macOS: Seatbelt (sandbox-exec)
# ---------------------------------------------------------------------------

class DarwinSeatbeltSandbox(ShellSandbox):
    """Seatbelt sandbox via ``sandbox-exec`` (Apple's sandbox framework).

    Write-only restriction, mirroring Windows Low-IL NO_WRITE_UP (and bwrap's
    read-only root): the profile leaves every operation at its default
    (reads, process exec, network — the honest omission shared across all
    backends) and restricts ONLY writes:

        (allow default)                          everything else unchanged
        (deny file-write*)                       no writes anywhere
        (allow file-write* ws tmp grants…)       except the writable roots
        (deny file-write* (subpath STATE))       <cwd>/.cluxmate re-denied

    ``(allow default)`` is REQUIRED: once a Seatbelt profile contains any rule,
    unmentioned operations (``process-exec``, ``file-read*``, network) default
    to DENY — without it even ``/bin/sh`` fails to exec with "Operation not
    permitted". It is the Homebrew-build-sandbox pattern, same as bwrap's
    read-only root + writable binds.

    Rule semantics are last-match-wins, so the specific ``STATE`` deny after
    the broad allow re-denies the agent's own permission/config subtree even
    though it sits inside the writable workspace — matching
    ``WriteFence.denyroots`` and the Windows medium re-label.

    Paths are injected as ``-D`` profile PARAMETERS (``(param "…")``), never
    string-interpolated into the scheme source, so a path containing spaces,
    quotes, or parens cannot break out of a ``subpath`` filter. ``sandbox-exec``
    is deprecated since macOS ~11 but still ships and works; ``available()``
    probes it read-only so removal degrades to fail-closed, not a crash.
    """

    name = "seatbelt"
    enforcement = "same-kernel"

    def __init__(self, grant_paths: list[str] | None = None):
        self._grant_paths = grant_paths or []

    @classmethod
    def available(cls) -> bool:
        return platform.system() == "Darwin" and shutil.which("sandbox-exec") is not None

    def _profile(self) -> str:
        """Scheme source. Writable roots are ``(param …)`` refs (see _prefix)."""
        allow_filters = ['(subpath (param "WS"))', '(subpath (param "TMP"))']
        for i in range(len(self._grant_paths)):
            allow_filters.append(f'(subpath (param "GRANT{i}"))')
        return (
            "(version 1)\n"
            "(allow default)\n"
            "(deny file-write*)\n"
            f'(allow file-write* {" ".join(allow_filters)})\n'
            '(deny file-write* (subpath (param "STATE")))\n'
        )

    def _profile_params(self, cwd: str) -> list[str]:
        """``-D key=value`` pairs the profile's ``(param "…")`` refs resolve.

        ``STATE`` is ``<cwd>/.cluxmate`` (the deny subtree, resolved so a
        symlinked workspace can't dodge it).
        """
        ws = str(Path(cwd).resolve())
        tmp = str(Path(tempfile.gettempdir()).resolve())
        state = str((Path(ws) / ".cluxmate").resolve())
        params = [f"WS={ws}", f"TMP={tmp}", f"STATE={state}"]
        for i, g in enumerate(self._grant_paths):
            params.append(f"GRANT{i}={str(Path(g).resolve())}")
        return params

    def _prefix(self, cwd: str) -> list[str]:
        """``sandbox-exec`` argv up to (not including) the child command.

        ``-D`` params carry the resolved paths verbatim; ``-p`` takes the
        profile inline. No ``--`` separator: ``sandbox-exec`` takes the
        command as the next argv element, and our commands are absolute
        paths (``/bin/sh`` or resolved executables), so there is no leading-
        dash ambiguity.
        """
        argv = ["sandbox-exec"]
        for p in self._profile_params(cwd):
            argv += ["-D", p]
        argv += ["-p", self._profile()]
        return argv

    def run(
        self,
        argv: list[str],
        *,
        shell_cmd: str | None,
        cwd: str,
        timeout: float,
        env: dict[str, str],
    ) -> SandboxResult:
        prefix = self._prefix(str(Path(cwd).resolve()))
        if shell_cmd is not None:
            full = prefix + ["/bin/sh", "-c", shell_cmd]
        else:
            full = prefix + argv
        proc = subprocess.run(
            full, capture_output=True, stdin=subprocess.DEVNULL,
            timeout=timeout, cwd=cwd, env=env,
        )
        return SandboxResult(proc.returncode, proc.stdout, proc.stderr)

    def spawn_popen(self, argv: list[str], *, cwd: str, env: dict[str, str]):
        prefix = self._prefix(str(Path(cwd).resolve()))
        return subprocess.Popen(
            prefix + argv,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=1, env=env, cwd=cwd,
            text=True, encoding="utf-8", errors="replace",
        )


# ---------------------------------------------------------------------------
# Windows: Low integrity-level token
# ---------------------------------------------------------------------------

_WIN_LOW_IL_SID = (0, 0, 0, 0, 0, 16)   # S-1-16 authority
_WIN_LOW_IL_LEVEL = 0x1000              # LOW mandatory level
_WIN_TOKEN_INTEGRITY_LEVEL = 25
_WIN_TOKEN_ALL_ACCESS = 0xF01FF
_WIN_SECURITY_IMPERSONATION = 2
_WIN_TOKEN_PRIMARY = 1
_WIN_STARTF_USESTDHANDLES = 0x00000100
_WIN_CREATE_NO_WINDOW = 0x08000000
_WIN_HANDLE_FLAG_INHERIT = 0x00000001
_WIN_INFINITE = 0xFFFFFFFF               # WaitForSingleObject: wait forever
_WIN_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_WIN_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002


def _win_kernel32():
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.GetCurrentProcess.restype = wintypes.HANDLE
    k.SetHandleInformation.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
    ]
    # PROC_THREAD_ATTRIBUTE_LIST plumbing — argtypes are mandatory here: the
    # pointer/size args are 64-bit and default (int) marshalling truncates
    # them, silently corrupting the attribute list on Win64.
    k.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    k.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    k.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p,     # lpAttributeList
        wintypes.DWORD,      # dwFlags
        ctypes.c_size_t,     # Attribute (DWORD_PTR)
        ctypes.c_void_p,     # lpValue
        ctypes.c_size_t,     # cbSize
        ctypes.c_void_p,     # lpPreviousValue
        ctypes.c_void_p,     # lpReturnSize
    ]
    k.UpdateProcThreadAttribute.restype = wintypes.BOOL
    k.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    k.DeleteProcThreadAttributeList.restype = None
    return k


def _handle_value(h) -> int:
    """Normalize a handle (raw int from msvcrt.get_osfhandle, or a
    ctypes HANDLE/c_void_p instance) to a plain int for a HANDLE array."""
    import ctypes

    if isinstance(h, ctypes.c_void_p):
        return int(h.value or 0)
    return int(h)


def _win_advapi32():
    import ctypes
    from ctypes import wintypes

    a = ctypes.WinDLL("advapi32", use_last_error=True)
    a.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    ]
    return a


class WindowsLowILSandbox(ShellSandbox):
    """Low-IL restricted token via Win32 (no admin required).

    Per-workspace setup (idempotent, marker-cached under <ws>/.cluxmate):
      1. icacls <ws> /setintegritylevel (OI)(CI)L /T   — workspace writable
         by the low-IL child (mandatory label inherits to new files).
      2. icacls <ws>/.cluxmate /setintegritylevel (OI)(CI)M /T — the agent's
         own permission/config subtree stays medium → NO_WRITE_UP blocks the
         sandboxed shell from editing it (mirrors WriteFence.denyroots).

    Each command runs under a duplicated token whose integrity level is
    lowered to LOW. stdin is NUL; stdout/stderr are redirect files opened by
    the (medium-IL) parent — the mandatory-label check applies at open time,
    so inherited handles let the low-IL child write its output to the normal
    temp dir without escaping the boundary.

    Partial by construction: reads and network remain unrestricted.
    """

    name = "windows-lowil"
    enforcement = "partial"

    def __init__(self, workspace: str, grant_paths: list[str] | None = None):
        self._workspace = str(Path(workspace).resolve())
        self._grant_paths = grant_paths or []
        self._setup_done = False

    @classmethod
    def available(cls) -> bool:
        if platform.system() != "Windows":
            return False
        try:
            import ctypes  # noqa: F401
            return shutil.which("icacls") is not None
        except Exception:
            return False

    # -- setup -----------------------------------------------------------

    def _marker(self) -> Path:
        return Path(self._workspace) / ".cluxmate" / "sandbox-il-applied"

    def _scratch(self) -> Path:
        """Low-labeled scratch the sandboxed child uses as TMP/TEMP.

        %TEMP% is medium-IL — a low-IL child cannot create files there (the
        label check fires at open time in the child), which breaks every
        toolchain that stages temp files (npm, pip, ...). This directory
        lives under the (denied-to-file-tools) .cluxmate subtree but is
        explicitly labeled LOW, after the subtree is re-raised medium.
        """
        return Path(self._workspace) / ".cluxmate" / "tmp-low"

    def _label_dirs_low(self, root: Path, skip_state: bool = False) -> None:
        """Label a tree's DIRECTORIES low-IL (incremental, no file walk).

        (OI)(CI) inheritance makes new files inherit LOW; existing files keep
        their old label (a low-IL child writing them fails closed). When
        ``skip_state``, the ``.cluxmate`` subtree is left untouched (it is
        re-raised medium separately for the workspace's own state).
        """
        root = Path(root)
        subprocess.run(
            ["icacls", str(root), "/setintegritylevel", "(OI)(CI)L", "/C", "/Q"],
            capture_output=True, timeout=60,
        )
        for dirpath, dirnames, _files in os.walk(root):
            rel = os.path.relpath(dirpath, root)
            if skip_state and (rel == ".cluxmate" or rel.startswith(".cluxmate" + os.sep)):
                dirnames[:] = []  # don't descend into the state subtree
                continue
            for d in dirnames:
                subprocess.run(
                    ["icacls", os.path.join(dirpath, d),
                     "/setintegritylevel", "(OI)(CI)L", "/C", "/Q"],
                    capture_output=True, timeout=30,
                )

    @staticmethod
    def _integrity_level(path: Path) -> str | None:
        """Read a path's CURRENT mandatory integrity label, or None if absent.

        Returns the label's first letter ("L"/"M"/"H"/"S"), or None when no
        label is present (an unlabeled object defaults to Medium on Windows).
        Used by _setup to VERIFY the real label instead of trusting the stale
        marker: a re-label back to medium leaves the marker behind and silently
        breaks the sandbox (the low-IL child can then write nothing).
        """
        try:
            r = subprocess.run(
                ["icacls", str(path)], capture_output=True, timeout=30,
            )
        except Exception:
            return None
        if r.returncode != 0:
            return None
        out = r.stdout.decode("utf-8", errors="replace")
        m = re.search(
            r"Mandatory Label\\(Low|Medium|High|System) Mandatory Level", out
        )
        if not m:
            return None
        return m.group(1)[0]

    def _setup(self) -> None:
        """Label the workspace + grant folders low-IL (self-healing).

        The marker is a HINT, not a guarantee. A re-label of the workspace
        back to medium (restore_path, a re-clone, an outer harness re-ACLing
        the tree) leaves the marker stale and silently breaks the sandbox —
        the low-IL child can then write nothing and pytest dies at tempfile
        discovery. So verify the REAL label and re-apply only on drift.
        """
        if self._setup_done:
            return
        ws = Path(self._workspace)
        state = ws / ".cluxmate"
        state.mkdir(parents=True, exist_ok=True)

        # Workspace drift: unlabeled == medium == drifted. Re-label the tree,
        # re-raise the deny subtree to medium, refresh the marker.
        if self._integrity_level(ws) != "L":
            self._label_dirs_low(ws, skip_state=True)
            r2 = subprocess.run(
                ["icacls", str(state), "/setintegritylevel", "(OI)(CI)M",
                 "/C", "/Q"],
                capture_output=True, timeout=60,
            )
            # r2 failing is not fatal — worst case the deny subtree keeps its
            # inherited low label; surface it on stderr.
            if r2.returncode != 0:
                import sys
                print(
                    f"[sandbox] warning: could not re-raise {state} to medium IL: "
                    f"{r2.stderr.decode(errors='replace').strip()}",
                    file=sys.stderr,
                )
            self._marker().write_text("low-il setup complete\n", encoding="utf-8")

        # User-granted folders: label low on every setup (idempotent — a grant
        # added since the last build is picked up here).
        for g in self._grant_paths:
            gp = Path(g)
            if gp.is_dir():
                self._label_dirs_low(gp)

        # Scratch for the child's TMP/TEMP: must exist AND be low-labeled. Its
        # label can drift like the workspace's (exists-but-medium), so verify
        # it too rather than only creating when missing.
        scratch = self._scratch()
        if not scratch.exists():
            scratch.mkdir(parents=True, exist_ok=True)
        if self._integrity_level(scratch) != "L":
            subprocess.run(
                ["icacls", str(scratch), "/setintegritylevel", "(OI)(CI)L",
                 "/C", "/Q"],
                capture_output=True, timeout=60,
            )
        self._setup_done = True

    @staticmethod
    def restore_path(path: str) -> bool:
        """Restore a folder from Low back to Medium (revocation reconcile).

        /T walks files too: files the low-IL child CREATED during the grant
        period inherited LOW and must be re-raised. Returns False on failure.
        """
        try:
            r = subprocess.run(
                ["icacls", str(Path(path).resolve()),
                 "/setintegritylevel", "(OI)(CI)M", "/T", "/C", "/Q"],
                capture_output=True, timeout=600,
            )
            return r.returncode == 0
        except Exception:
            return False

    # -- process creation -------------------------------------------------

    def _low_token(self):
        """Duplicate our own token with the integrity level lowered to LOW."""
        import ctypes
        from ctypes import wintypes

        advapi32 = _win_advapi32()
        kernel32 = _win_kernel32()

        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(),
            _WIN_TOKEN_ALL_ACCESS,
            ctypes.byref(token),
        ):
            raise SandboxUnavailable(
                f"OpenProcessToken failed (WinError {ctypes.get_last_error()})"
            )
        new_token = wintypes.HANDLE()
        if not advapi32.DuplicateTokenEx(
            token,
            _WIN_TOKEN_ALL_ACCESS,
            None,
            _WIN_SECURITY_IMPERSONATION,
            _WIN_TOKEN_PRIMARY,
            ctypes.byref(new_token),
        ):
            raise SandboxUnavailable(
                f"DuplicateTokenEx failed (WinError {ctypes.get_last_error()}"
            )
        kernel32.CloseHandle(token)

        # AllocateAndInitializeSid(S-1-16, 1 subauth: LOW) → TOKEN_MANDATORY_LABEL
        sid = ctypes.c_void_p()
        label_authority = (ctypes.c_ubyte * 6)(*_WIN_LOW_IL_SID)
        if not advapi32.AllocateAndInitializeSid(
            ctypes.byref(label_authority),
            1,
            _WIN_LOW_IL_LEVEL, 0, 0, 0, 0, 0, 0, 0,
            ctypes.byref(sid),
        ):
            kernel32.CloseHandle(new_token)
            raise SandboxUnavailable("AllocateAndInitializeSid failed")

        class TOKEN_MANDATORY_LABEL(ctypes.Structure):
            _fields_ = [("Label", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

        tml = TOKEN_MANDATORY_LABEL(sid, 0)
        if not advapi32.SetTokenInformation(
            new_token,
            _WIN_TOKEN_INTEGRITY_LEVEL,
            ctypes.byref(tml),
            ctypes.sizeof(tml),
        ):
            kernel32.CloseHandle(new_token)
            raise SandboxUnavailable(
                "SetTokenInformation(TokenIntegrityLevel) failed"
            )
        return new_token, sid

    def _startup(self, stdin_handle, out_handle, err_handle):
        """Build a STARTUPINFOEXW wiring the three std handles AND constraining
        inheritance to exactly those handles via PROC_THREAD_ATTRIBUTE_HANDLE_LIST.

        Without the handle list, ``CreateProcessAsUserW(bInheritHandles=True)``
        leaks EVERY inheritable handle currently open in the process to the
        child. Since bash runs and MCP/LSP spawns execute concurrently on the
        executor thread pool, one spawn would inherit another's pipe/redirect
        handles — breaking sandbox isolation and (for pipes) deadlocking the
        parent's read because a stray child keeps the write end open past EOF.
        The handle list makes inheritance deterministic per-spawn.

        Returns ``(startupex, keepalive)``; the caller must keep ``keepalive``
        referenced until CreateProcess returns and then call
        ``_free_attr_list(keepalive)`` to release the attribute list.
        """
        import ctypes
        from ctypes import wintypes

        kernel32 = _win_kernel32()

        class STARTUPINFOW(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR),
                ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD),
                ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.c_void_p),
                ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE),
                ("hStdError", wintypes.HANDLE),
            ]

        class STARTUPINFOEXW(ctypes.Structure):
            _fields_ = [
                ("StartupInfo", STARTUPINFOW),
                ("lpAttributeList", ctypes.c_void_p),
            ]

        six = STARTUPINFOEXW()
        si = six.StartupInfo
        si.cb = ctypes.sizeof(STARTUPINFOEXW)
        si.dwFlags = _WIN_STARTF_USESTDHANDLES
        si.hStdInput = stdin_handle
        si.hStdOutput = out_handle
        si.hStdError = err_handle

        # Deduplicate: UpdateProcThreadAttribute rejects a handle list with
        # repeats (e.g. stdout==stderr would fail the whole spawn).
        seen: list[int] = []
        for h in (stdin_handle, out_handle, err_handle):
            v = _handle_value(h)
            if v and v not in seen:
                seen.append(v)
        count = len(seen)
        handle_arr = (wintypes.HANDLE * count)(*seen)

        # Size probe: first call fails with ERROR_INSUFFICIENT_BUFFER and fills
        # in the required size.
        size = ctypes.c_size_t(0)
        kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value)
        attr_list = ctypes.cast(buf, ctypes.c_void_p)
        if not kernel32.InitializeProcThreadAttributeList(
            attr_list, 1, 0, ctypes.byref(size)
        ):
            raise OSError(
                f"InitializeProcThreadAttributeList failed "
                f"(WinError {ctypes.get_last_error()})"
            )
        if not kernel32.UpdateProcThreadAttribute(
            attr_list, 0, _WIN_PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
            ctypes.cast(handle_arr, ctypes.c_void_p),
            ctypes.sizeof(handle_arr), None, None,
        ):
            err = ctypes.get_last_error()
            kernel32.DeleteProcThreadAttributeList(attr_list)
            raise OSError(f"UpdateProcThreadAttribute failed (WinError {err})")
        six.lpAttributeList = attr_list

        # buf + handle_arr must outlive CreateProcess; attr_list must be freed.
        keepalive = (buf, handle_arr, attr_list)
        return six, keepalive

    @staticmethod
    def _free_attr_list(keepalive) -> None:
        """Release the attribute list built in _startup (idempotent-safe)."""
        if not keepalive:
            return
        attr_list = keepalive[2]
        try:
            _win_kernel32().DeleteProcThreadAttributeList(attr_list)
        except Exception:
            pass

    def run(
        self,
        argv: list[str],
        *,
        shell_cmd: str | None,
        cwd: str,
        timeout: float,
        env: dict[str, str],
    ) -> SandboxResult:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        self._setup()

        kernel32 = _win_kernel32()
        advapi32 = _win_advapi32()

        # The child's TMP/TEMP must point at the low-labeled scratch — the
        # medium-labeled %TEMP% is unwritable for a low-IL process.
        env = dict(env)
        scratch = str(self._scratch())
        env["TMP"] = scratch
        env["TEMP"] = scratch
        env["TMPDIR"] = scratch

        if shell_cmd is not None:
            cmdline = subprocess.list2cmdline(["cmd.exe", "/d", "/c", shell_cmd])
        else:
            cmdline = subprocess.list2cmdline(argv)

        # Parent-opened redirect files: the mandatory-label check happens at
        # open time in the medium-IL parent; the child writes through
        # INHERITED handles, which bypass the label check by design.
        out_fd, out_path = tempfile.mkstemp(prefix="cluxmate-sb-", suffix=".out")
        err_fd, err_path = tempfile.mkstemp(prefix="cluxmate-sb-", suffix=".err")
        null_fd = os.open(os.devnull, os.O_RDONLY)
        try:
            handles = []
            for fd in (out_fd, err_fd, null_fd):
                h = msvcrt.get_osfhandle(fd)
                # mkstemp/os.open handles are NOT inheritable by default;
                # the child needs them marked inheritable.
                if not kernel32.SetHandleInformation(
                    h, _WIN_HANDLE_FLAG_INHERIT, _WIN_HANDLE_FLAG_INHERIT
                ):
                    raise OSError("SetHandleInformation failed")
                handles.append(h)
            out_h, err_h, null_h = handles

            new_token, sid = self._low_token()
            startup, keepalive = self._startup(null_h, out_h, err_h)
            try:
                class PROCESS_INFORMATION(ctypes.Structure):
                    _fields_ = [
                        ("hProcess", wintypes.HANDLE),
                        ("hThread", wintypes.HANDLE),
                        ("dwProcessId", wintypes.DWORD),
                        ("dwThreadId", wintypes.DWORD),
                    ]

                pi = PROCESS_INFORMATION()
                # Explicit unicode environment block (CREATE_UNICODE_ENVIRONMENT)
                # so the TMP/TEMP overrides reach the child. EXTENDED_STARTUPINFO
                # activates the handle-list attribute built in _startup.
                env_block = ctypes.create_unicode_buffer(
                    "".join(f"{k}={v}\0" for k, v in env.items()) + "\0"
                )
                ok = advapi32.CreateProcessAsUserW(
                    new_token,
                    None,
                    cmdline,
                    None,
                    None,
                    True,                      # inherit handles (limited to the list)
                    _WIN_CREATE_NO_WINDOW | 0x00000400  # NO_WINDOW | UNICODE_ENV
                    | _WIN_EXTENDED_STARTUPINFO_PRESENT,
                    env_block,
                    cwd,
                    ctypes.byref(startup),
                    ctypes.byref(pi),
                )
                if not ok:
                    raise OSError(
                        f"CreateProcessAsUserW failed "
                        f"(WinError {ctypes.get_last_error()})"
                    )
                try:
                    timed_out = (
                        kernel32.WaitForSingleObject(
                            pi.hProcess, int(timeout * 1000)
                        ) != 0
                    )
                    if timed_out:
                        kernel32.TerminateProcess(pi.hProcess, 1)
                        kernel32.WaitForSingleObject(pi.hProcess, 10000)
                    rc = wintypes.DWORD()
                    kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(rc))
                finally:
                    kernel32.CloseHandle(pi.hThread)
                    kernel32.CloseHandle(pi.hProcess)
            finally:
                advapi32.FreeSid(sid)
                kernel32.CloseHandle(new_token)
                self._free_attr_list(keepalive)

            out = Path(out_path).read_bytes()
            err = Path(err_path).read_bytes()
            rc_final = rc.value if not timed_out else 1
            if timed_out:
                err += f"\n[sandbox] command timed out after {timeout}s".encode()
            return SandboxResult(rc_final, out, err)
        finally:
            os.close(out_fd)
            os.close(err_fd)
            os.close(null_fd)
            Path(out_path).unlink(missing_ok=True)
            Path(err_path).unlink(missing_ok=True)


    def spawn_popen(
        self,
        argv: list[str],
        *,
        cwd: str,
        env: dict[str, str],
        encoding: str = "utf-8",
        errors: str = "replace",
    ) -> "_LowILProcess":
        """Spawn a LONG-RUNNING low-IL child with PIPE stdio (MCP servers).

        Returns a Popen-shaped object exposing the subset MCPClient uses:
        ``stdin`` (text write, line-buffered), ``stdout`` (readline),
        ``poll()``, ``wait(timeout)``, ``kill()``, ``returncode``, ``pid``.
        Anonymous pipes bypass the mandatory-label check (they aren't file
        objects), so the low-IL child communicates freely while remaining
        unable to open medium-IL files itself.
        """
        import ctypes
        import msvcrt
        from ctypes import wintypes

        self._setup()
        kernel32 = _win_kernel32()
        advapi32 = _win_advapi32()

        env = dict(env)
        scratch = str(self._scratch())
        env["TMP"] = scratch
        env["TEMP"] = scratch
        env["TMPDIR"] = scratch

        cmdline = subprocess.list2cmdline(argv)

        kernel32.CreatePipe.argtypes = [
            ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE),
            ctypes.c_void_p, wintypes.DWORD,
        ]

        def _pipe():
            r = wintypes.HANDLE()
            w = wintypes.HANDLE()
            if not kernel32.CreatePipe(ctypes.byref(r), ctypes.byref(w), None, 0):
                raise OSError(f"CreatePipe failed (WinError {ctypes.get_last_error()})")
            return r, w

        # child_stdin: parent writes in_w, child reads in_r
        # child_stdout: child writes out_w, parent reads out_r
        # child_stderr: DEVNULL (matches MCP's stderr=DEVNULL — an unread
        # stderr pipe would fill and block the long-lived server)
        in_r, in_w = _pipe()
        out_r, out_w = _pipe()

        null_fd = os.open(os.devnull, os.O_RDWR)
        null_h = msvcrt.get_osfhandle(null_fd)
        if not kernel32.SetHandleInformation(
            null_h, _WIN_HANDLE_FLAG_INHERIT, _WIN_HANDLE_FLAG_INHERIT
        ):
            raise OSError("SetHandleInformation failed")

        # Only the CHILD-facing ends are inheritable.
        for h in (in_r, out_w):
            if not kernel32.SetHandleInformation(
                h, _WIN_HANDLE_FLAG_INHERIT, _WIN_HANDLE_FLAG_INHERIT
            ):
                raise OSError("SetHandleInformation failed")

        new_token, sid = self._low_token()
        startup, keepalive = self._startup(in_r, out_w, null_h)
        try:
            class PROCESS_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("hProcess", wintypes.HANDLE),
                    ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD),
                    ("dwThreadId", wintypes.DWORD),
                ]

            pi = PROCESS_INFORMATION()
            env_block = ctypes.create_unicode_buffer(
                "".join(f"{k}={v}\0" for k, v in env.items()) + "\0"
            )
            ok = advapi32.CreateProcessAsUserW(
                new_token,
                None,
                cmdline,
                None,
                None,
                True,                      # inherit handles (limited to the list)
                _WIN_CREATE_NO_WINDOW | 0x00000400
                | _WIN_EXTENDED_STARTUPINFO_PRESENT,
                env_block,
                cwd,
                ctypes.byref(startup),
                ctypes.byref(pi),
            )
            if not ok:
                raise OSError(
                    f"CreateProcessAsUserW failed "
                    f"(WinError {ctypes.get_last_error()})"
                )
            kernel32.CloseHandle(pi.hThread)
        finally:
            advapi32.FreeSid(sid)
            kernel32.CloseHandle(new_token)
            self._free_attr_list(keepalive)
            # Parent closes the child-facing ends; the child holds its own
            # inherited copies. Keep only our communication ends.
            kernel32.CloseHandle(in_r)
            kernel32.CloseHandle(out_w)
            os.close(null_fd)

        return _LowILProcess(
            hprocess=pi.hProcess,
            pid=pi.dwProcessId,
            stdin_w=in_w,
            stdout_r=out_r,
            encoding=encoding,
            errors=errors,
        )


class _LowILProcess:
    """Popen-shaped wrapper over a low-IL child + inherited anonymous pipes.

    Implements exactly the surface MCPClient uses. File objects wrap the
    parent's pipe ends via msvcrt.open_osfhandle + os.fdopen (text, line
    buffered), matching subprocess.Popen(text=True, bufsize=1).
    """

    def __init__(self, hprocess, pid, stdin_w, stdout_r, encoding, errors):
        import msvcrt
        import os as _os

        self.pid = int(pid)
        self.returncode = None
        self._handle = hprocess
        self.args = None

        def _open(handle, mode):
            fd = msvcrt.open_osfhandle(handle.value, _os.O_TEXT)
            return _os.fdopen(fd, mode, encoding=encoding, errors=errors,
                              newline="\n", buffering=1)

        self.stdin = _open(stdin_w, "w")
        self.stdout = _open(stdout_r, "r")
        self.stderr = None  # DEVNULL — nothing to read

    def _get_rc(self) -> int | None:
        import ctypes
        kernel32 = _win_kernel32()
        rc = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(self._handle, ctypes.byref(rc))
        return rc.value

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        import ctypes
        kernel32 = _win_kernel32()
        if kernel32.WaitForSingleObject(self._handle, 0) == 0:
            self.returncode = self._get_rc()
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        import ctypes
        kernel32 = _win_kernel32()
        ms = _WIN_INFINITE if timeout is None else int(timeout * 1000)
        if kernel32.WaitForSingleObject(self._handle, ms) != 0:
            # WAIT_TIMEOUT → raise subprocess-style timeout so callers'
            # existing `except subprocess.TimeoutExpired` path still works.
            raise subprocess.TimeoutExpired(self.args or [], timeout or 0.0)
        self.returncode = self._get_rc()
        return self.returncode

    def kill(self) -> None:
        self.terminate()

    def terminate(self) -> None:
        import ctypes
        kernel32 = _win_kernel32()
        kernel32.TerminateProcess(self._handle, 1)
        kernel32.WaitForSingleObject(self._handle, 10000)
        self.returncode = self._get_rc()


# ---------------------------------------------------------------------------
# Probe chain + fail-closed
# ---------------------------------------------------------------------------

def sandbox_disabled_by_env() -> bool:
    return os.environ.get(ENV_DISABLE, "").lower() in ("off", "0", "disabled", "false")


def pick_sandbox(workspace: str, grant_paths: list[str] | None = None) -> ShellSandbox | None:
    """Return the first AVAILABLE backend for this platform, or None.

    Probe order per platform (single-candidate today, chain-ready). Probing
    is read-only; per-workspace setup (icacls labeling) is deferred to run.
    ``grant_paths`` are the user-granted extra writable folders.
    """
    system = platform.system()
    candidates: list[Any] = []
    if system == "Linux":
        candidates = [BwrapSandbox]
    elif system == "Darwin":
        candidates = [DarwinSeatbeltSandbox]
    elif system == "Windows":
        candidates = [WindowsLowILSandbox]
    for cls in candidates:
        if cls.available():
            if cls is WindowsLowILSandbox:
                return cls(workspace, grant_paths=grant_paths)
            return cls(grant_paths=grant_paths)
    return None
