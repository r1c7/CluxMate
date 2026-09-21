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

import os
import platform
import signal
import subprocess
import threading
from typing import Any, Callable

#: Deadline for the drain AFTER the kill. Bounded on purpose: a descendant that
#: escaped the tree kill can hold the write end open forever, and an unbounded
#: drain is the very hang this module exists to prevent.
#: Worst case for one call is therefore ``timeout + 15 s (taskkill, Windows
#: only) + KILL_DRAIN_SECONDS`` — see kill_process_tree for why taskkill's own
#: subprocess.run(timeout=…) cannot stall.
KILL_DRAIN_SECONDS = 5.0

def kill_process_tree(pid: int) -> None:
    """Kill ``pid`` and the processes it spawned (best effort, never raises).

    Windows: ``taskkill /F /T`` walks the parent/child tree. A low-integrity
    child is terminable from this higher-integrity parent — the mandatory-label
    policy only forbids writes UP, never termination DOWN. POSIX: the child owns
    its process group (``start_new_session`` in :func:`run_bounded`), so one
    ``SIGKILL`` to the group takes the shell and everything under it, and
    bubblewrap's ``--die-with-parent`` then finishes its sandboxed child.

    The ``taskkill`` call is a ``subprocess.run(timeout=15)``: that is safe HERE
    (unlike for a command) because taskkill spawns no children, so nothing
    survives to hold ITS pipes open — the unbounded post-kill ``communicate()``
    CPython falls back to always returns. It does add up to 15 s to the worst
    case, which is why :func:`run_bounded`'s bound is stated as
    ``timeout + taskkill + drain``.

    Residual (documented, not fixed): a descendant that was REPARENTED before
    the kill is no longer in the tree ``taskkill`` walks, and one that started a
    NEW session left the group.
    """
    if platform.system() == "Windows":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, stdin=subprocess.DEVNULL, timeout=15,
            )
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

    The return is bounded by ``timeout + kill + drain`` (the kill may spend up
    to 15 s in ``taskkill`` on Windows, see :func:`kill_process_tree`); it never
    waits for the command itself once the timeout has passed.

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
