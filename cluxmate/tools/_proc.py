"""Bounded child-process execution and cancellation.

Every place that spawns a shell child — the bare bash path (``tools/bash.py``)
and the OS sandbox backends (``tools/_sandbox.py``) — needs the same two
guarantees, and they are subtle enough to live in exactly one implementation:

* **A timeout MUST reach the caller.** ``subprocess.run(timeout=…)`` cannot
  provide that: on Windows its timeout branch kills ONLY the direct child and
  then calls ``communicate()`` AGAIN with no timeout to collect the output
  (CPython ``subprocess.py``:559, "communicate() _after_ kill() is required to
  collect that"). A descendant still holding stdout's write end stalls that read
  indefinitely, so the timeout never surfaces — one real tool call hung 88
  minutes that way and "[Command timed out …]" was never emitted.
* **The command's process TREE must die with it.** Killing the shell alone
  leaves the actual work running, and nothing else reaps it: the executor thread
  that started it is abandoned both on timeout and when a turn is cancelled.
"""

from __future__ import annotations

import ctypes
import os
import platform
import signal
import subprocess
import threading
import time
from typing import Any, Callable

#: Deadline for the drain AFTER the kill. Bounded on purpose: a descendant that
#: escaped the tree kill can hold the write end open forever, and an unbounded
#: drain is the very hang this module exists to prevent.
#: Worst case for one call is therefore ``timeout + TREE_KILL_SECONDS (Windows
#: only) + KILL_DRAIN_SECONDS`` — see :func:`kill_process_tree` for why the tree
#: kill spends a confirmation window of its own.
KILL_DRAIN_SECONDS = 5.0

#: Windows only: how long ONE tree kill may spend confirming the tree is gone.
#: It replaces ``taskkill``, whose own worst case was a 15 s subprocess — so the
#: ceiling of a call went DOWN even though the kill now polls.
TREE_KILL_SECONDS = 2.0

#: Windows only: gap between two snapshots of the tree.
_TREE_POLL_SECONDS = 0.05

#: Windows only: how long the tree must stay EMPTY before the kill is believed.
#: "Nothing there right now" is not yet "nothing there": a process terminated a
#: moment ago can still complete the CreateProcess it already had in flight.
_TREE_QUIET_SECONDS = 0.4

_PROCESS_TERMINATE = 0x0001
_SYNCHRONIZE = 0x00100000
_TH32CS_SNAPPROCESS = 0x00000002
_WAIT_OBJECT_0 = 0x00000000
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class _ProcessEntry32(ctypes.Structure):
    """``PROCESSENTRY32`` (ANSI) — only the pid pair is read, but the layout has
    to match what the API writes, hence every field and its natural alignment.
    ``th32DefaultHeapID`` is ``ULONG_PTR``: pointer-sized, not a DWORD."""

    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_char * 260),
    ]


_kernel32_lib: Any = None


def _kernel32() -> Any:
    """kernel32 with the five entry points this module needs, bound once.

    Lazy on purpose: ``ctypes.WinDLL`` does not exist off Windows, so the module
    stays importable (and the POSIX path stays untouched) everywhere.
    """
    global _kernel32_lib
    if _kernel32_lib is None:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        k32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        k32.Process32First.restype = ctypes.c_int
        k32.Process32First.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_ProcessEntry32)
        ]
        k32.Process32Next.restype = ctypes.c_int
        k32.Process32Next.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_ProcessEntry32)
        ]
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k32.TerminateProcess.restype = ctypes.c_int
        k32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.WaitForSingleObject.restype = ctypes.c_uint32
        k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.CloseHandle.restype = ctypes.c_int
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        _kernel32_lib = k32
    return _kernel32_lib


def _win_open(pid: int, access: int) -> int | None:
    handle = _kernel32().OpenProcess(access, False, pid)
    return int(handle) if handle else None


def _win_exited(handle: int) -> bool:
    return _kernel32().WaitForSingleObject(handle, 0) == _WAIT_OBJECT_0


def _win_process_table() -> dict[int, int] | None:
    """``{pid: parent pid}`` for every process, or None if the snapshot failed.

    ``th32ParentProcessID`` is the pid that CREATED the process; Windows freezes
    it, so an orphan still points at a parent that may be long dead. That is the
    link :func:`_kill_tree_windows` walks — the one a ``taskkill /T`` snapshot
    cannot follow once the parent is gone.
    """
    k32 = _kernel32()
    snap = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snap or int(snap) == _INVALID_HANDLE:
        return None
    try:
        entry = _ProcessEntry32()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32)
        if not k32.Process32First(snap, ctypes.byref(entry)):
            return None
        table: dict[int, int] = {}
        while True:
            table[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            if not k32.Process32Next(snap, ctypes.byref(entry)):
                break
        return table
    finally:
        k32.CloseHandle(snap)


def _kill_tree_windows(pid: int) -> None:
    """Kill ``pid`` and everything below it, then confirm the tree stayed dead.

    ``taskkill /F /T`` — what this used to be — takes ONE snapshot of the tree
    and then kills what it saw. A descendant created between that snapshot and
    the kill is missed, and missed FOR GOOD: its parent is dead, so a later
    ``taskkill /PID`` has no tree left to walk. That is not theoretical. The
    stale-epoch path in :func:`run_bounded` kills a child in the very
    milliseconds it is starting its own children, and CI caught a writer
    grandchild outliving its command that way, still writing minutes later.

    So the tree is not snapshotted once, it is FOLLOWED. Every poll re-reads the
    process table and adopts anything whose parent pid is already in the tree —
    the parent may have died since, which is exactly the case that has to be
    caught; then every adopted process is terminated, and the kill is over only
    once each one has exited and nothing new appeared for
    ``_TREE_QUIET_SECONDS`` (itself bounded by ``TREE_KILL_SECONDS``).

    Each adopted pid is held open for the whole walk. That is what makes the
    "is it gone" test exact — a terminated process that still has an open handle
    stays visible to a snapshot, but is signalled here — and it is what stops an
    adopted pid from being recycled into an unrelated process we would then kill.

    Residual (documented, not fixed): the walk can only descend through a pid it
    has SEEN. An intermediate that starts and exits before the walk reads the
    table — or between two polls — was never adopted, so the children it left
    behind are outside the walk: that is the shape of
    ``test_bash_timeout_stays_bounded_when_a_descendant_escapes_the_kill``. And a
    process whose ``OpenProcess`` is denied can be neither killed nor watched; it
    is retried every poll and the loop simply runs to the deadline.
    """
    k32 = _kernel32()
    access = _PROCESS_TERMINATE | _SYNCHRONIZE
    root = _win_open(pid, access)
    if root is None:  # already gone, or not ours to touch
        return
    tracked: dict[int, int] = {pid: root}  # pid -> open handle (pins the pid)
    try:
        deadline = time.monotonic() + TREE_KILL_SECONDS
        quiet_since: float | None = None
        while True:
            table = _win_process_table()
            for child, parent in (table or {}).items():
                if parent in tracked and child not in tracked:
                    handle = _win_open(child, access)
                    if handle is not None:
                        tracked[child] = handle
            for handle in tracked.values():
                k32.TerminateProcess(handle, 1)  # idempotent: retries a lost try
            now = time.monotonic()
            if all(_win_exited(handle) for handle in tracked.values()):
                quiet_since = now if quiet_since is None else quiet_since
                if now - quiet_since >= _TREE_QUIET_SECONDS:
                    return
            else:
                quiet_since = None
            if now >= deadline:
                return
            time.sleep(_TREE_POLL_SECONDS)
    finally:
        for handle in tracked.values():
            k32.CloseHandle(handle)


def kill_process_tree(pid: int) -> None:
    """Kill ``pid`` and the processes it spawned (best effort, never raises).

    Windows: walk the tree through ``th32ParentProcessID`` and terminate what it
    holds, repeatedly, until the tree is confirmed gone —
    :func:`_kill_tree_windows` documents why one snapshot is not enough. A
    low-integrity child is terminable from this higher-integrity parent: the
    mandatory-label policy only forbids writes UP, never termination DOWN.
    POSIX: the child owns its process group (``start_new_session`` in
    :func:`run_bounded`), so one ``SIGKILL`` to the group takes the shell and
    everything under it, and bubblewrap's ``--die-with-parent`` then finishes its
    sandboxed child.

    Residual (documented, not fixed): on POSIX a descendant that started a NEW
    session left the group; on Windows a descendant is out of reach when the pid
    that created it was never observed by the walk (see
    :func:`_kill_tree_windows`).
    """
    if platform.system() == "Windows":
        try:
            _kill_tree_windows(pid)
        except Exception:
            pass
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except Exception:
        pass


class LiveProcesses:
    """The child processes one tool still has running.

    A cancelled turn abandons the executor thread blocked on the child, so the
    only thread that can still stop the work is the one ending the turn:
    ``BaseTool.cancel_running`` calls :meth:`cancel_all` from there.

    The epoch closes the spawn race without latching: a runtime reads
    :meth:`epoch` BEFORE spawning and passes it to :meth:`add`, which answers
    False when a cancel landed in between — then the caller kills that child
    itself. A latch would be simpler but would also kill every command of the
    NEXT turn, since one tool instance serves a whole session.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._kills: dict[int, Callable[[], None]] = {}
        self._epoch = 0

    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    def add(self, epoch: int, pid: int, kill: Callable[[], None]) -> bool:
        """Track a child; False = cancelled since ``epoch`` (the caller kills).

        A stale ``epoch`` is NOT tracked: the child is about to be killed, and
        leaving it in the registry would make a later cancel re-kill a dead pid.
        """
        with self._lock:
            if epoch != self._epoch:
                return False
            self._kills[pid] = kill
            return True

    def discard(self, pid: int) -> None:
        with self._lock:
            self._kills.pop(pid, None)

    def cancel_all(self) -> int:
        """Kill every tracked child tree. Returns how many were live."""
        with self._lock:
            self._epoch += 1
            kills = list(self._kills.values())
            self._kills.clear()
        for kill in kills:
            try:
                kill()
            except Exception:
                pass
        return len(kills)


def run_bounded(
    args: str | list[str],
    *,
    shell: bool,
    cwd: str,
    env: dict[str, str],
    timeout: float,
    live: LiveProcesses | None = None,
) -> tuple[int | None, bytes, bytes]:
    """Run ``args`` to completion within ``timeout``; ALWAYS returns.

    Returns ``(returncode, stdout, stderr)``, where a ``None`` returncode means
    the command did not finish — it timed out (and was killed), or its tool was
    cancelled while it was starting. Either way the caller must not read the
    output as a completed run.

    The return is bounded by ``timeout + kill + drain`` (on Windows the kill may
    spend up to ``TREE_KILL_SECONDS`` confirming the tree, see
    :func:`kill_process_tree`); it never waits for the command itself once the
    timeout has passed.

    ``live`` registers the child so a cancelled turn can kill it
    (:class:`LiveProcesses`); the sandbox backends take the same registry from
    their ``run(live=…)`` argument.
    """
    kwargs: dict[str, Any] = {
        "cwd": cwd,
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if platform.system() != "Windows":
        # Own process group, so one killpg can reach the whole tree.
        kwargs["start_new_session"] = True
    epoch = live.epoch() if live is not None else 0
    proc = subprocess.Popen(args, shell=shell, **kwargs)
    try:
        if live is not None and not live.add(
            epoch, proc.pid, lambda: kill_process_tree(proc.pid)
        ):
            # The turn was cancelled between our epoch read and this spawn: this
            # child must not outlive the turn that asked for it.
            return _kill_and_drain(proc)
        try:
            out, err = proc.communicate(timeout=timeout)
            return proc.returncode, out, err
        except subprocess.TimeoutExpired:
            return _kill_and_drain(proc)
    finally:
        if live is not None:
            live.discard(proc.pid)


def _kill_and_drain(proc: subprocess.Popen) -> tuple[None, bytes, bytes]:
    """Kill the process tree, then collect what the pipes still hold."""
    kill_process_tree(proc.pid)
    try:
        # Bounded by design: this read is what releases CPython's reader
        # threads, and KILL_DRAIN_SECONDS is the deadline on survivors that
        # escaped the kill.
        out, err = proc.communicate(timeout=KILL_DRAIN_SECONDS)
    except Exception:
        # A survivor that escaped the tree kill still holds the write end.
        # Give up on the output and return — do NOT try to close our pipe
        # ends here: the reader thread is parked inside ``fh.read()`` and holds
        # the buffered reader's lock, so ``stream.close()`` BLOCKS until that
        # survivor exits, reintroducing the hang this module prevents
        # (measured). Residue: those reader threads and handles live until the
        # survivor dies.
        out, err = b"", b""
    return None, out, err
