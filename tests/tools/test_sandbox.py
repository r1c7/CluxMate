"""Tests for shell sandbox runners (phase 1) + BashTool wiring.

Windows Low-IL tests are real end-to-end runs (label a temp workspace, spawn
a low-IL child, assert NO_WRITE_UP). They are skipped when icacls/ctypes are
unavailable. macOS Seatbelt tests are real end-to-end runs skipped off-Darwin;
their profile/argv construction is unit-tested everywhere. Linux bwrap tests
run end-to-end when bubblewrap is on PATH (skipped otherwise); argv
construction is unit-tested everywhere.
"""

import os
import platform
import sys
import tempfile
from pathlib import Path

import pytest

from cluxmate.tools._sandbox import (
    ENV_DISABLE,
    BwrapSandbox,
    DarwinSeatbeltSandbox,
    SandboxUnavailable,
    WindowsLowILSandbox,
    pick_sandbox,
    sandbox_disabled_by_env,
)
from cluxmate.tools.bash import BashTool

sys.stdout.reconfigure(errors="replace")  # GBK console safety

IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"
# bwrap end-to-end tests need a WORKING bubblewrap, not just the binary on PATH.
# GitHub-hosted ubuntu runners install bwrap but restrict unprivileged user
# namespaces, so `bwrap … true` fails with "setting up uid map: Permission
# denied" — probe once and skip rather than failing the whole matrix.
def _bwrap_usable() -> bool:
    if not BwrapSandbox.available():
        return False
    import subprocess
    try:
        r = subprocess.run(
            ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
             "--die-with-parent", "true"],
            capture_output=True, timeout=30,
        )
        return r.returncode == 0
    except Exception:
        return False


HAS_BWRAP = _bwrap_usable()


# ---------------------------------------------------------------------------
# Probe chain / fail-closed semantics
# ---------------------------------------------------------------------------

def test_sandbox_disabled_by_env(monkeypatch):
    monkeypatch.delenv(ENV_DISABLE, raising=False)
    assert sandbox_disabled_by_env() is False
    for v in ("off", "0", "disabled", "false", "OFF"):
        monkeypatch.setenv(ENV_DISABLE, v)
        assert sandbox_disabled_by_env() is True


def test_pick_sandbox_returns_backend_on_windows():
    if not IS_WIN:
        pytest.skip("windows-only")
    sb = pick_sandbox("C:/")
    assert isinstance(sb, WindowsLowILSandbox)


def test_bashtool_fail_closed_without_backend():
    tool = BashTool(workdir=".", sandbox=None, sandbox_required=True)
    import asyncio
    result = asyncio.run(tool.execute(command="echo hi"))
    assert "refused" in result
    assert "CLUXMATE_BASH_SANDBOX" in result
    assert "[exit code" not in result  # never executed


def test_bashtool_runs_normally_when_sandbox_not_required():
    # sandbox=None + sandbox_required=False = legacy bare mode (explicit
    # opt-out, e.g. CLUXMATE_BASH_SANDBOX=off or yolo).
    tool = BashTool(workdir=".")
    import asyncio
    result = asyncio.run(tool.execute(command="echo ok-bare"))
    assert "ok-bare" in result


# ---------------------------------------------------------------------------
# bwrap: argv construction (unit level)
# ---------------------------------------------------------------------------

def test_bwrap_argv_layout():
    sb = BwrapSandbox()
    argv = sb._bwrap_argv("/home/u/ws")
    assert argv[0] == "bwrap"
    assert "--ro-bind" in argv
    # workspace bind-writable: the two tokens after the first --bind are the
    # same path (bind src == dst). resolve() may rewrite it on Windows, so
    # assert src == dst rather than the literal.
    i = argv.index("--bind")
    assert argv[i + 1] == argv[i + 2]
    assert "--die-with-parent" in argv
    assert "--new-session" in argv


# ---------------------------------------------------------------------------
# Windows Low-IL: end-to-end
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not IS_WIN, reason="windows-only")
def test_lowil_write_inside_workspace_blocked_outside():
    ws = Path(tempfile.mkdtemp(prefix="cluxmate-sbtest-"))
    try:
        sb = WindowsLowILSandbox(str(ws))
        # 1) write inside workspace succeeds
        r = sb.run(argv=[], shell_cmd="echo ws > ok.txt", cwd=str(ws),
                   timeout=60, env=os.environ.copy())
        assert r.returncode == 0, r.stderr.decode(errors="replace")
        assert (ws / "ok.txt").exists()
        # 2) write to home (medium IL) is denied by NO_WRITE_UP
        escape = Path.home() / "cluxmate-sbtest-escape.txt"
        r2 = sb.run(
            argv=[], shell_cmd=f'echo bad > "{escape}"', cwd=str(ws),
            timeout=60, env=os.environ.copy(),
        )
        assert r2.returncode != 0
        assert not escape.exists()
    finally:
        import shutil
        shutil.rmtree(ws, ignore_errors=True)


@pytest.mark.skipif(not IS_WIN, reason="windows-only")
def test_lowil_deny_subtree_for_cluxmate_state():
    ws = Path(tempfile.mkdtemp(prefix="cluxmate-sbtest-"))
    try:
        sb = WindowsLowILSandbox(str(ws))
        r = sb.run(
            argv=[], shell_cmd="echo hacked > .cluxmate\\permissions.json",
            cwd=str(ws), timeout=60, env=os.environ.copy(),
        )
        assert r.returncode != 0
        assert not (ws / ".cluxmate" / "permissions.json").exists()
    finally:
        import shutil
        shutil.rmtree(ws, ignore_errors=True)


@pytest.mark.skipif(not IS_WIN, reason="windows-only")
def test_lowil_child_tmp_is_writable():
    ws = Path(tempfile.mkdtemp(prefix="cluxmate-sbtest-"))
    try:
        sb = WindowsLowILSandbox(str(ws))
        r = sb.run(
            argv=[], shell_cmd="echo t > %TEMP%\\child.txt", cwd=str(ws),
            timeout=60, env=os.environ.copy(),
        )
        assert r.returncode == 0, r.stderr.decode(errors="replace")
        assert (ws / ".cluxmate" / "tmp-low" / "child.txt").exists()
    finally:
        import shutil
        shutil.rmtree(ws, ignore_errors=True)


@pytest.mark.skipif(not IS_WIN, reason="windows-only")
def test_lowil_wait_infinite_does_not_raise():
    # Regression: _LowILProcess.wait(timeout=None) used an undefined
    # _WIN_INFINITE and raised NameError. It must block-then-return the code.
    ws = Path(tempfile.mkdtemp(prefix="cluxmate-sbtest-"))
    try:
        sb = WindowsLowILSandbox(str(ws))
        proc = sb.spawn_popen(["cmd.exe", "/d", "/c", "echo done"],
                              cwd=str(ws), env=os.environ.copy())
        assert proc.stdout.readline().strip() == "done"
        assert proc.wait(timeout=None) == 0  # no NameError, real exit code
    finally:
        import shutil
        shutil.rmtree(ws, ignore_errors=True)


@pytest.mark.skipif(not IS_WIN, reason="windows-only")
def test_lowil_concurrent_spawn_no_handle_leak():
    # Regression: CreateProcessAsUserW(bInheritHandles=True) without a
    # PROC_THREAD_ATTRIBUTE_HANDLE_LIST leaks every inheritable handle to each
    # child, so concurrent spawns inherit each other's stdout write-end and the
    # parent's readline() never sees EOF (deadlock) — and sandbox isolation
    # breaks. With the handle list, inheritance is per-spawn: each child's pipe
    # closes on its own exit, so the second readline() returns '' (EOF).
    import threading

    ws = Path(tempfile.mkdtemp(prefix="cluxmate-sbtest-"))
    try:
        sb = WindowsLowILSandbox(str(ws))
        sb._setup()  # one-time icacls labeling before the threads race
        n = 8
        results: list = [None] * n
        errors: list = [None] * n

        def worker(i):
            try:
                proc = sb.spawn_popen(
                    ["cmd.exe", "/d", "/c", f"echo line-{i}"],
                    cwd=str(ws), env=os.environ.copy(),
                )
                line = proc.stdout.readline().strip()
                eof = proc.stdout.readline()  # '' only if no sibling holds the write end
                rc = proc.wait(timeout=10)
                results[i] = (line, eof, rc)
            except Exception as e:  # noqa: BLE001
                errors[i] = repr(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=25)

        assert not any(t.is_alive() for t in threads), "spawn deadlocked (handle leak)"
        for i in range(n):
            assert errors[i] is None, f"worker {i}: {errors[i]}"
            line, eof, rc = results[i]
            assert line == f"line-{i}"
            assert eof == "", f"worker {i} got no EOF -> inherited sibling's pipe: {eof!r}"
            assert rc == 0
    finally:
        import shutil
        shutil.rmtree(ws, ignore_errors=True)


@pytest.mark.skipif(not IS_WIN, reason="windows-only")
def test_bashtool_end_to_end_under_sandbox():
    ws = Path(tempfile.mkdtemp(prefix="cluxmate-sbtest-"))
    try:
        import asyncio
        tool = BashTool(workdir=str(ws),
                        sandbox=WindowsLowILSandbox(str(ws)),
                        sandbox_required=True)
        result = asyncio.run(tool.execute(command="echo sandboxed-echo"))
        assert "sandboxed-echo" in result
    finally:
        import shutil
        shutil.rmtree(ws, ignore_errors=True)


@pytest.mark.skipif(not IS_WIN, reason="windows-only")
def test_granted_folder_writable_then_restored():
    import shutil
    ws = Path(tempfile.mkdtemp(prefix="cluxmate-sbtest-"))
    granted = Path(tempfile.mkdtemp(prefix="cluxmate-grant-"))
    try:
        sb = WindowsLowILSandbox(str(ws), grant_paths=[str(granted)])
        # 1) Granted folder is writable by the low-IL child.
        #    NOTE: no double-quotes around the redirect target — cmd.exe under
        #    a CREATE_NO_WINDOW low-IL spawn fails the redirect when quoted.
        target = granted / "granted.txt"
        r = sb.run(argv=[], shell_cmd=f'echo ok > {target}', cwd=str(ws),
                   timeout=60, env=os.environ.copy())
        assert r.returncode == 0, r.stderr.decode(errors="replace")
        assert target.exists()
        # 2) restore_path raises it back to medium → low-IL child can't write.
        assert WindowsLowILSandbox.restore_path(str(granted)) is True
        target2 = granted / "after-restore.txt"
        r2 = sb.run(argv=[], shell_cmd=f'echo no > {target2}', cwd=str(ws),
                    timeout=60, env=os.environ.copy())
        assert r2.returncode != 0
        assert not target2.exists()
    finally:
        shutil.rmtree(ws, ignore_errors=True)
        shutil.rmtree(granted, ignore_errors=True)


def test_bwrap_binds_grant_paths():
    sb = BwrapSandbox(grant_paths=["/mnt/data"])
    argv = sb._bwrap_argv("/home/u/ws")
    # Each grant appears as a --bind pair (resolved). Assert the RESOLVED
    # grant path is bound: resolve() rewrites it on Windows.
    resolved = str(Path("/mnt/data").resolve())
    # Count of --bind pairs went from 2 (ws + temp) to 3 with one grant.
    assert argv.count("--bind") == 3
    assert resolved in argv


def test_bwrap_deny_subtree_ro_bind(monkeypatch):
    # <cwd>/.cluxmate is re-mounted read-only AFTER every writable bind (last
    # mount wins) — mirroring WriteFence.denyroots / Windows medium re-label /
    # Seatbelt STATE. is_dir is faked (rather than creating a real dir) so the
    # test is hermetic even in constrained temp environments; the real
    # end-to-end deny is covered by test_bwrap_deny_subtree_for_cluxmate_state.
    real_is_dir = Path.is_dir
    monkeypatch.setattr(
        Path, "is_dir",
        lambda self: True if self.name == ".cluxmate" else real_is_dir(self),
    )
    sb = BwrapSandbox(grant_paths=["/mnt/data"])
    argv = sb._bwrap_argv("/home/u/ws")
    state = str((Path("/home/u/ws").resolve() / ".cluxmate").resolve())
    i = argv.index(state)
    assert argv[i - 1] == "--ro-bind"    # re-mounted read-only, not --bind
    assert argv[i + 1] == state          # src == dst
    # The deny bind is LAST: after the temp, workspace, and grant --bind pairs.
    for idx in (argv.index(str(Path(tempfile.gettempdir()).resolve())),
                argv.index(str(Path("/home/u/ws").resolve())),
                argv.index(str(Path("/mnt/data").resolve()))):
        assert idx < i


def test_bwrap_tmp_bound_before_workspace():
    # Mount order: temp dir FIRST, then workspace — a workspace under the temp
    # dir (tests mkdtemp there) must not be shadowed by the temp bind.
    sb = BwrapSandbox()
    argv = sb._bwrap_argv("/home/u/ws")
    tmp = str(Path(tempfile.gettempdir()).resolve())
    ws = str(Path("/home/u/ws").resolve())
    assert argv.index(tmp) < argv.index(ws)


def test_bwrap_no_deny_bind_when_state_missing():
    # Without .cluxmate, no --ro-bind of it (bwrap fails on a missing source);
    # run()/spawn_popen() create it first via _ensure_state_dir.
    sb = BwrapSandbox()
    argv = sb._bwrap_argv("/home/u/ws")
    assert str((Path("/home/u/ws").resolve() / ".cluxmate").resolve()) not in argv


# ---------------------------------------------------------------------------
# Linux bwrap: end-to-end (requires the real bubblewrap binary)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HAS_BWRAP, reason="linux-only (needs bubblewrap)")
def test_bwrap_write_inside_workspace_blocked_outside():
    import shutil
    ws = Path(tempfile.mkdtemp(prefix="cluxmate-sbtest-"))
    try:
        sb = BwrapSandbox()
        # 1) write inside workspace succeeds
        r = sb.run(argv=[], shell_cmd="echo ws > ok.txt", cwd=str(ws),
                   timeout=60, env=os.environ.copy())
        assert r.returncode == 0, r.stderr.decode(errors="replace")
        assert (ws / "ok.txt").exists()
        # 2) write to home is denied (read-only root bind)
        escape = Path.home() / "cluxmate-sbtest-escape.txt"
        r2 = sb.run(argv=[], shell_cmd=f'echo bad > "{escape}"', cwd=str(ws),
                    timeout=60, env=os.environ.copy())
        assert r2.returncode != 0
        assert not escape.exists()
    finally:
        shutil.rmtree(ws, ignore_errors=True)


@pytest.mark.skipif(not HAS_BWRAP, reason="linux-only (needs bubblewrap)")
def test_bwrap_deny_subtree_for_cluxmate_state():
    import shutil
    ws = Path(tempfile.mkdtemp(prefix="cluxmate-sbtest-"))
    try:
        sb = BwrapSandbox()
        # _ensure_state_dir creates .cluxmate, which _bwrap_argv then ro-binds;
        # a write to permissions.json must fail on the read-only mount.
        r = sb.run(argv=[], shell_cmd="echo hacked > .cluxmate/permissions.json",
                   cwd=str(ws), timeout=60, env=os.environ.copy())
        assert r.returncode != 0
        assert not (ws / ".cluxmate" / "permissions.json").exists()
    finally:
        shutil.rmtree(ws, ignore_errors=True)


@pytest.mark.skipif(not HAS_BWRAP, reason="linux-only (needs bubblewrap)")
def test_bwrap_granted_folder_writable():
    import shutil
    ws = Path(tempfile.mkdtemp(prefix="cluxmate-sbtest-"))
    granted = Path(tempfile.mkdtemp(prefix="cluxmate-grant-"))
    try:
        sb = BwrapSandbox(grant_paths=[str(granted)])
        target = granted / "granted.txt"
        r = sb.run(argv=[], shell_cmd=f'echo ok > "{target}"', cwd=str(ws),
                   timeout=60, env=os.environ.copy())
        assert r.returncode == 0, r.stderr.decode(errors="replace")
        assert target.exists()
    finally:
        shutil.rmtree(ws, ignore_errors=True)
        shutil.rmtree(granted, ignore_errors=True)


@pytest.mark.skipif(not HAS_BWRAP, reason="linux-only (needs bubblewrap)")
def test_bwrap_tmp_is_writable():
    import shutil
    ws = Path(tempfile.mkdtemp(prefix="cluxmate-sbtest-"))
    try:
        sb = BwrapSandbox()
        tmp = Path(tempfile.gettempdir()).resolve()
        target = tmp / "cluxmate-sbtest-child.txt"
        r = sb.run(argv=[], shell_cmd=f'echo t > "{target}"', cwd=str(ws),
                   timeout=60, env=os.environ.copy())
        assert r.returncode == 0, r.stderr.decode(errors="replace")
        assert target.exists()
        target.unlink(missing_ok=True)
    finally:
        shutil.rmtree(ws, ignore_errors=True)


# ---------------------------------------------------------------------------
# macOS Seatbelt: profile/argv construction (unit level) + end-to-end
# ---------------------------------------------------------------------------

def test_seatbelt_profile_layout():
    sb = DarwinSeatbeltSandbox(grant_paths=["/mnt/data"])
    profile = sb._profile()
    assert "(version 1)" in profile
    # Write-only restriction, in last-match-wins order: allow everything by
    # default (Seatbelt is deny-by-default once any rule exists), then deny all
    # writes, allow the writable roots, then re-deny the state subtree.
    assert profile.index("(allow default)") < profile.index("(deny file-write*)")
    assert profile.index("(deny file-write*)") < profile.index("(allow file-write*")
    assert profile.index("(allow file-write*") < profile.index(
        '(deny file-write* (subpath (param "STATE")))'
    )
    assert '(subpath (param "WS"))' in profile
    assert '(subpath (param "TMP"))' in profile
    assert '(subpath (param "GRANT0"))' in profile


def test_seatbelt_prefix_layout():
    sb = DarwinSeatbeltSandbox(grant_paths=["/mnt/data"])
    prefix = sb._prefix("/Users/u/ws")
    assert prefix[0] == "sandbox-exec"
    # -D params carry resolved absolute paths verbatim (never scheme-interpolated).
    params: dict[str, str] = {}
    i = 1
    while i < len(prefix) and prefix[i] == "-D":
        k, v = prefix[i + 1].split("=", 1)
        params[k] = v
        i += 2
    assert params["WS"] == str(Path("/Users/u/ws").resolve())
    assert params["STATE"] == str((Path("/Users/u/ws").resolve() / ".cluxmate").resolve())
    assert params["TMP"] == str(Path(tempfile.gettempdir()).resolve())
    assert params["GRANT0"] == str(Path("/mnt/data").resolve())
    # Profile follows the -D pairs via -p.
    assert prefix[i] == "-p"
    assert prefix[i + 1] == sb._profile()


def test_pick_sandbox_returns_seatbelt_on_darwin(monkeypatch):
    import cluxmate.tools._sandbox as sb_mod
    monkeypatch.setattr(sb_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sb_mod.DarwinSeatbeltSandbox, "available",
                        classmethod(lambda cls: True))
    sb = sb_mod.pick_sandbox("/Users/u/ws")
    assert isinstance(sb, DarwinSeatbeltSandbox)


def test_pick_sandbox_none_on_darwin_without_backend(monkeypatch):
    # Fail-closed: on macOS without sandbox-exec, pick_sandbox returns None and
    # BashTool refuses (sandbox_required=True) rather than running bare.
    import cluxmate.tools._sandbox as sb_mod
    monkeypatch.setattr(sb_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sb_mod.DarwinSeatbeltSandbox, "available",
                        classmethod(lambda cls: False))
    assert sb_mod.pick_sandbox("/Users/u/ws") is None


@pytest.mark.skipif(not IS_MAC, reason="macos-only")
def test_seatbelt_write_inside_workspace_blocked_outside():
    ws = Path(tempfile.mkdtemp(prefix="cluxmate-sbtest-"))
    try:
        sb = DarwinSeatbeltSandbox()
        # 1) write inside workspace succeeds
        r = sb.run(argv=[], shell_cmd="echo ws > ok.txt", cwd=str(ws),
                   timeout=60, env=os.environ.copy())
        assert r.returncode == 0, r.stderr.decode(errors="replace")
        assert (ws / "ok.txt").exists()
        # 2) write to home is denied (Operation not permitted)
        escape = Path.home() / "cluxmate-sbtest-escape.txt"
        r2 = sb.run(argv=[], shell_cmd=f'echo bad > "{escape}"', cwd=str(ws),
                    timeout=60, env=os.environ.copy())
        assert r2.returncode != 0
        assert not escape.exists()
    finally:
        import shutil
        shutil.rmtree(ws, ignore_errors=True)


@pytest.mark.skipif(not IS_MAC, reason="macos-only")
def test_seatbelt_deny_subtree_for_cluxmate_state():
    ws = Path(tempfile.mkdtemp(prefix="cluxmate-sbtest-"))
    try:
        sb = DarwinSeatbeltSandbox()
        # The dir must exist so the shell reaches the sandbox's write-denial
        # (rather than failing on a missing parent before any I/O).
        (ws / ".cluxmate").mkdir(parents=True, exist_ok=True)
        r = sb.run(argv=[], shell_cmd="echo hacked > .cluxmate/permissions.json",
                   cwd=str(ws), timeout=60, env=os.environ.copy())
        assert r.returncode != 0
        assert not (ws / ".cluxmate" / "permissions.json").exists()
    finally:
        import shutil
        shutil.rmtree(ws, ignore_errors=True)


@pytest.mark.skipif(not IS_MAC, reason="macos-only")
def test_seatbelt_granted_folder_writable():
    ws = Path(tempfile.mkdtemp(prefix="cluxmate-sbtest-"))
    granted = Path(tempfile.mkdtemp(prefix="cluxmate-grant-"))
    try:
        sb = DarwinSeatbeltSandbox(grant_paths=[str(granted)])
        target = granted / "granted.txt"
        r = sb.run(argv=[], shell_cmd=f'echo ok > "{target}"', cwd=str(ws),
                   timeout=60, env=os.environ.copy())
        assert r.returncode == 0, r.stderr.decode(errors="replace")
        assert target.exists()
    finally:
        import shutil
        shutil.rmtree(ws, ignore_errors=True)
        shutil.rmtree(granted, ignore_errors=True)


# ---------------------------------------------------------------------------
# Access-denied hint (failure experience)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not IS_WIN, reason="windows-only")
def test_bashtool_surfaces_denial_hint_on_access_denied():
    import shutil
    import asyncio
    ws = Path(tempfile.mkdtemp(prefix="cluxmate-sbtest-"))
    try:
        tool = BashTool(
            workdir=str(ws),
            sandbox=WindowsLowILSandbox(str(ws)),
            sandbox_required=True,
        )
        escape = Path.home() / "cluxmate-sbtest-escape.txt"
        result = asyncio.run(
            tool.execute(command=f"echo bad > {escape}")
        )
        # The low-IL child is denied (NO_WRITE_UP) and the hint is appended,
        # pointing at the ONE structured next step (escalation).
        assert "[sandbox: access denied]" in result
        assert "sandbox_permissions" in result and "danger-full-access" in result
        assert not escape.exists()
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_access_denied_regex_matches_common_fingerprints():
    from cluxmate.tools.bash import _ACCESS_DENIED_RE
    assert _ACCESS_DENIED_RE.search("Access is denied.")
    assert _ACCESS_DENIED_RE.search("Permission denied")
    assert _ACCESS_DENIED_RE.search("Read-only file system")
    assert _ACCESS_DENIED_RE.search("Operation not permitted")
    assert _ACCESS_DENIED_RE.search("拒绝访问")
    # Negative: unrelated text must NOT match.
    assert not _ACCESS_DENIED_RE.search("command not found")


@pytest.mark.skipif(not IS_WIN, reason="windows-only")
def test_bashtool_no_hint_on_normal_failure():
    import shutil
    import asyncio
    ws = Path(tempfile.mkdtemp(prefix="cluxmate-sbtest-"))
    try:
        tool = BashTool(
            workdir=str(ws),
            sandbox=WindowsLowILSandbox(str(ws)),
            sandbox_required=True,
        )
        # A failing-but-not-access-denied command (exit code != 0) must NOT
        # get the sandbox hint — the heuristic must not over-fire.
        result = asyncio.run(tool.execute(command="exit 7"))
        assert "[exit code: 7]" in result
        assert "[sandbox: access denied]" not in result
    finally:
        shutil.rmtree(ws, ignore_errors=True)


# ---------------------------------------------------------------------------
# Windows Low-IL: label-drift self-healing (unit level, icacls mocked)
# ---------------------------------------------------------------------------

def _fake_run_capture(monkeypatch, calls):
    """Route subprocess.run through a recorder; icacls argv is logged."""
    import types

    import cluxmate.tools._sandbox as sb_mod

    def fake_run(argv, **kw):
        if argv and argv[0] == "icacls" and "/setintegritylevel" in argv:
            calls.append(" ".join(argv))
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(sb_mod.subprocess, "run", fake_run)


@pytest.mark.skipif(not IS_WIN, reason="windows-only")
def test_lowil_setup_heals_drifted_workspace(monkeypatch, tmp_path):
    """A stale marker must NOT mask a workspace re-labeled back to medium.

    The marker is a hint; the real label is what matters. When the workspace
    root is no longer LOW, _setup must re-label the tree and refresh the
    marker even though the marker already exists (the old code trusted the
    marker and skipped the workspace entirely — the exact drift that leaves a
    low-IL child with nothing writable).
    """
    sb = WindowsLowILSandbox(str(tmp_path))
    # Pre-seed the stale marker so the marker-exists branch is under test.
    marker = tmp_path / ".cluxmate" / "sandbox-il-applied"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("stale\n", encoding="utf-8")

    label_calls = []
    icacls_calls = []

    def fake_integrity(path):
        # Workspace drifted to medium (unlabeled); scratch is healthy low.
        return None if Path(path).name != "tmp-low" else "L"

    monkeypatch.setattr(sb, "_integrity_level", fake_integrity)
    monkeypatch.setattr(
        sb, "_label_dirs_low",
        lambda root, skip_state=False: label_calls.append((str(root), skip_state)),
    )
    _fake_run_capture(monkeypatch, icacls_calls)

    sb._setup()

    # The drifted workspace must be re-labeled despite the existing marker.
    assert any(Path(r) == Path(tmp_path) and skip for r, skip in label_calls)
    # The marker must be refreshed with fresh content.
    assert "low-il setup complete" in marker.read_text(encoding="utf-8")
    # The deny subtree must be re-raised to medium.
    assert any("(OI)(CI)M" in c for c in icacls_calls)


@pytest.mark.skipif(not IS_WIN, reason="windows-only")
def test_lowil_setup_heals_drifted_scratch(monkeypatch, tmp_path):
    """An existing-but-medium tmp-low must be re-labeled low.

    The old code only labeled the scratch when it was MISSING; a scratch that
    exists but was re-raised to medium stayed broken, so the child's TMP/TEMP
    was unwritable and pytest died at tempfile discovery.
    """
    sb = WindowsLowILSandbox(str(tmp_path))
    scratch = tmp_path / ".cluxmate" / "tmp-low"
    scratch.mkdir(parents=True, exist_ok=True)  # exists, but drifted to medium

    label_calls = []
    icacls_calls = []

    def fake_integrity(path):
        # Workspace healthy low; the scratch drifted (unlabeled = medium).
        return "L" if Path(path).name != "tmp-low" else None

    monkeypatch.setattr(sb, "_integrity_level", fake_integrity)
    monkeypatch.setattr(
        sb, "_label_dirs_low",
        lambda root, skip_state=False: label_calls.append(str(root)),
    )
    _fake_run_capture(monkeypatch, icacls_calls)

    sb._setup()

    # The healthy workspace must NOT be re-labeled.
    assert label_calls == []
    # The drifted scratch must be re-labeled low exactly once.
    scratch_low = [c for c in icacls_calls if "tmp-low" in c and "(OI)(CI)L" in c]
    assert len(scratch_low) == 1


@pytest.mark.skipif(not IS_WIN, reason="windows-only")
def test_lowil_setup_noop_when_labels_healthy(monkeypatch, tmp_path):
    """When workspace + scratch are already LOW, _setup must not relabel.

    Idempotence guard: no wasted icacls tree walk on every session start.
    """
    sb = WindowsLowILSandbox(str(tmp_path))

    label_calls = []
    icacls_calls = []

    monkeypatch.setattr(sb, "_integrity_level", lambda path: "L")
    monkeypatch.setattr(
        sb, "_label_dirs_low",
        lambda root, skip_state=False: label_calls.append(str(root)),
    )
    _fake_run_capture(monkeypatch, icacls_calls)

    sb._setup()

    assert label_calls == []      # workspace NOT re-labeled
    assert icacls_calls == []     # no setintegritylevel at all


@pytest.mark.skipif(not IS_WIN, reason="windows-only")
def test_lowil_setup_reverifies_after_first_call(monkeypatch, tmp_path):
    """A drift that happens AFTER the first _setup must still be healed.

    Regression: _setup short-circuited on a one-shot _setup_done flag, so a
    workspace re-ACLed back to medium mid-session (outer harness, re-clone,
    restore_path) was never re-detected — the low-IL child then had nothing
    writable and every write forced a danger-full-access escalation. Each call
    must re-verify the real labels instead of trusting the first setup.
    """
    sb = WindowsLowILSandbox(str(tmp_path))
    (tmp_path / ".cluxmate" / "tmp-low").mkdir(parents=True, exist_ok=True)

    label_calls = []
    icacls_calls = []
    monkeypatch.setattr(
        sb, "_label_dirs_low",
        lambda root, skip_state=False: label_calls.append((str(root), skip_state)),
    )
    _fake_run_capture(monkeypatch, icacls_calls)

    # First call: everything already low — a pure no-op.
    monkeypatch.setattr(sb, "_integrity_level", lambda path: "L")
    sb._setup()
    assert label_calls == []
    assert icacls_calls == []

    # Mid-session drift: the outer harness re-ACLs the whole tree to medium.
    monkeypatch.setattr(sb, "_integrity_level", lambda path: None)
    sb._setup()

    # Workspace re-labeled low (skip_state=True), scratch re-labeled low, and
    # the deny subtree re-raised to medium.
    assert any(Path(r) == tmp_path and skip for r, skip in label_calls)
    assert any("tmp-low" in c and "(OI)(CI)L" in c for c in icacls_calls)
    assert any("(OI)(CI)M" in c for c in icacls_calls)


@pytest.mark.skipif(not IS_WIN, reason="windows-only")
def test_lowil_setup_backs_off_after_failed_heal(monkeypatch, tmp_path):
    """A heal that fails (labels can't be set) must not re-walk every command.

    icacls can exit 0 on denial, so _setup re-reads the label AFTER the
    attempt. When it stays non-LOW, the expensive full-tree walk backs off for
    _LABEL_RETRY_INTERVAL seconds instead of re-running on every sandboxed
    command — a broken environment (icacls denied) would otherwise pay O(dirs)
    icacls per command.
    """
    sb = WindowsLowILSandbox(str(tmp_path))
    (tmp_path / ".cluxmate" / "tmp-low").mkdir(parents=True, exist_ok=True)

    label_calls = []
    icacls_calls = []
    monkeypatch.setattr(
        sb, "_label_dirs_low",
        lambda root, skip_state=False: label_calls.append((str(root), skip_state)),
    )
    _fake_run_capture(monkeypatch, icacls_calls)

    # Labels can never be set: every read reports medium (unlabeled).
    monkeypatch.setattr(sb, "_integrity_level", lambda path: None)

    sb._setup()
    assert len(label_calls) == 1  # workspace walked once on the first heal
    assert any("tmp-low" in c and "(OI)(CI)L" in c for c in icacls_calls)

    # Second call within the backoff window: the workspace must NOT be re-walked.
    sb._setup()
    assert len(label_calls) == 1


@pytest.mark.parametrize("label,letter", [
    ("Low", "L"), ("Medium", "M"), ("High", "H"), ("System", "S"),
])
def test_integrity_level_parses_icacls_label(monkeypatch, label, letter):
    """_integrity_level reads the Mandatory Label line out of icacls output."""
    import types

    import cluxmate.tools._sandbox as sb_mod

    def fake_run(argv, **kw):
        return types.SimpleNamespace(
            returncode=0,
            stdout=(
                f"{argv[1]} Mandatory Label\\{label} Mandatory Level:(OI)(CI)(NW)\n"
                f"NT AUTHORITY\\SYSTEM:(I)(F)\n"
            ).encode("utf-8"),
            stderr=b"",
        )

    monkeypatch.setattr(sb_mod.subprocess, "run", fake_run)
    assert WindowsLowILSandbox._integrity_level(Path("C:/x")) == letter


def test_integrity_level_none_when_unlabeled(monkeypatch):
    """An unlabeled object (default medium) parses to None, signalling drift."""
    import types

    import cluxmate.tools._sandbox as sb_mod

    def fake_run(argv, **kw):
        return types.SimpleNamespace(
            returncode=0,
            stdout=b"BUILTIN\\Users:(I)(RX)\nNT AUTHORITY\\SYSTEM:(I)(F)\n",
            stderr=b"",
        )

    monkeypatch.setattr(sb_mod.subprocess, "run", fake_run)
    assert WindowsLowILSandbox._integrity_level(Path("C:/x")) is None
