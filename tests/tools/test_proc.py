"""tools/_proc.py — the bounded-execution + cancellation contract.

Two guarantees are pinned here, because both backends (the bare bash path and
the OS sandboxes) rely on them:

  * a timeout ALWAYS returns, even when a descendant keeps stdout open, and
  * a cancelled call's process TREE is reachable and killable afterwards.
"""

import sys
import threading
import time

from cluxmate.tools._proc import LiveProcesses, kill_process_tree, run_bounded

# Appends a line every 50 ms: a live witness that outlives its parent.
_WRITER = """
import pathlib, sys, time
path = pathlib.Path(sys.argv[1])
for i in range(600):
    with path.open("a") as fh:
        fh.write(f"{i}\\n")
    time.sleep(0.05)
"""


def _write_script(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body)
    return path


def _wait_for_growth(path, *, timeout=15.0):
    """Wait until a writer has produced at least two distinct sizes."""
    deadline = time.monotonic() + timeout
    seen = -1
    while time.monotonic() < deadline:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size > seen >= 0:
            return size
        seen = max(seen, size)
        time.sleep(0.1)
    raise AssertionError(f"no writes seen in {path}")


def _assert_frozen(path, *, settle=1.0):
    """The writer must have stopped — its size is stable over two samples.

    Callers that use this as their ONLY proof of the tree kill must first assert
    the witness exists: an absent file means the writer never ran, and "never
    ran" is trivially frozen.
    """
    if not path.exists():
        return
    time.sleep(settle)
    first = path.read_text()
    time.sleep(settle)
    assert path.read_text() == first, "the writer was still alive"


# ---------------------------------------------------------------------------
# LiveProcesses
# ---------------------------------------------------------------------------

def test_add_after_cancel_reports_untracked():
    """The epoch check is what closes the spawn race without latching: a runtime
    reads epoch() BEFORE spawning, and add() answers False when a cancel landed
    in between — the caller then kills that child itself."""
    live = LiveProcesses()
    epoch = live.epoch()
    assert live.add(epoch, 4242, lambda: None) is True
    assert live.cancel_all() == 1
    # The same epoch is now stale: a child spawned from it must not be trusted.
    assert live.add(epoch, 4243, lambda: None) is False
    # ...while a runtime that starts AFTER the cancel is fine again (one tool
    # instance serves a whole session — a latch would kill every later turn).
    assert live.add(live.epoch(), 4244, lambda: None) is True


def test_cancel_all_runs_every_kill_once():
    live = LiveProcesses()
    killed: list[int] = []
    live.add(live.epoch(), 1, lambda: killed.append(1))
    live.add(live.epoch(), 2, lambda: killed.append(2))
    assert live.cancel_all() == 2
    assert sorted(killed) == [1, 2]
    # Idempotent: nothing is left to kill on a second pass.
    assert live.cancel_all() == 0


def test_cancel_all_survives_a_failing_kill():
    """Best-effort by contract: one tool's broken kill must not stop the others."""
    live = LiveProcesses()
    killed: list[int] = []

    def boom():
        raise OSError("nope")

    live.add(live.epoch(), 1, boom)
    live.add(live.epoch(), 2, lambda: killed.append(2))
    assert live.cancel_all() == 2
    assert killed == [2]


def test_discard_forgets_a_child():
    live = LiveProcesses()
    live.add(live.epoch(), 7, lambda: None)
    live.discard(7)
    assert live.cancel_all() == 0


# ---------------------------------------------------------------------------
# run_bounded
# ---------------------------------------------------------------------------

def test_run_bounded_returns_normally(tmp_path):
    rc, out, err = run_bounded(
        f'"{sys.executable}" -c "print(42)"', shell=True, cwd=str(tmp_path),
        env={"PATH": ""}, timeout=30,
    )
    assert rc == 0
    assert b"42" in out
    assert err == b""


def test_run_bounded_timeout_returns_and_kills_a_real_tree(tmp_path):
    """The regression this module exists for: the grandchild holds stdout's
    write end, so an unbounded post-kill drain never sees EOF."""
    child = _write_script(
        tmp_path, "child.py",
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
        "time.sleep(600)\n",
    )
    writer = _write_script(tmp_path, "writer.py", _WRITER)
    counter = tmp_path / "counter.txt"

    started = time.monotonic()
    rc, out, err = run_bounded(
        f'"{sys.executable}" "{child}" "{writer}" "{counter}"',
        shell=True, cwd=str(tmp_path), env={"PATH": ""}, timeout=3,
    )
    elapsed = time.monotonic() - started

    assert rc is None
    assert elapsed < 8, f"the timeout did not return promptly: {elapsed:.1f}s"
    # The kill is only proven if the writer actually ran (3 s is comfortably
    # more than the ~0.3 s it needs to start and write its first line).
    assert counter.exists(), "the writer never ran — nothing was proven"
    _assert_frozen(counter)


def test_run_bounded_kill_reaches_the_registry(tmp_path):
    """cancel_all() must stop a child that is ALREADY running (the turn-cancel
    path): the runtime blocks in run_bounded on another thread."""
    child = _write_script(
        tmp_path, "child.py",
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
        "time.sleep(600)\n",
    )
    writer = _write_script(tmp_path, "writer.py", _WRITER)
    counter = tmp_path / "counter.txt"
    live = LiveProcesses()

    outcome: dict = {}

    def _run():
        started = time.monotonic()
        outcome["result"] = run_bounded(
            f'"{sys.executable}" "{child}" "{writer}" "{counter}"',
            shell=True, cwd=str(tmp_path), env={"PATH": ""}, timeout=60,
            live=live,
        )
        outcome["elapsed"] = time.monotonic() - started

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    _wait_for_growth(counter)
    assert live.cancel_all() == 1
    thread.join(timeout=20)

    assert not thread.is_alive(), "cancel_all did not free the running call"
    assert outcome["elapsed"] < 20
    # Killed, not timed out: communicate() returns the shell's failure code
    # instead of our None marker (the turn is gone and discards the result).
    assert outcome["result"][0] not in (0, None)
    _assert_frozen(counter)


def test_cancel_does_not_poison_later_calls(tmp_path):
    """One BashTool instance serves a whole session, so a cancel must not latch:
    the NEXT turn's command has to run normally (its epoch is fresh)."""
    live = LiveProcesses()
    live.cancel_all()
    rc, out, err = run_bounded(
        f'"{sys.executable}" -c "print(11)"', shell=True, cwd=str(tmp_path),
        env={"PATH": ""}, timeout=30, live=live,
    )
    assert rc == 0
    assert b"11" in out


def test_run_bounded_cancel_between_epoch_and_spawn_kills_the_child(
    tmp_path, monkeypatch
):
    """The spawn race: the cancel lands after run_bounded read the epoch and
    before the child is registered. run_bounded must kill that child itself —
    it is not tracked, so cancel_all() can never reach it.

    The falsifier here is ``elapsed``: unfixed (the stale-epoch child is tracked
    and awaited) this call blocks for the child's full 600 s sleep, while the
    drain deadline is 5 s. The witness file only ADDS evidence when it exists —
    the child may legitimately die before it ever starts writing.
    """
    child = _write_script(
        tmp_path, "child.py",
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
        "time.sleep(600)\n",
    )
    writer = _write_script(tmp_path, "writer.py", _WRITER)
    counter = tmp_path / "counter.txt"
    live = LiveProcesses()

    # The cancel has to land exactly in the window Popen opens, so it is
    # injected at the spawn itself.
    import subprocess as sp

    real_popen = sp.Popen
    state = {"cancelled": False}

    def _popen_cancelling_first_time(*args, **kwargs):
        if not state["cancelled"]:
            state["cancelled"] = True
            live.cancel_all()
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(sp, "Popen", _popen_cancelling_first_time)
    started = time.monotonic()
    rc, out, err = run_bounded(
        f'"{sys.executable}" "{child}" "{writer}" "{counter}"',
        shell=True, cwd=str(tmp_path), env={"PATH": ""}, timeout=60, live=live,
    )
    elapsed = time.monotonic() - started
    monkeypatch.undo()

    assert rc is None, "a child spawned from a stale epoch must not be reported"
    assert elapsed < 20, f"the dropped child was not killed: {elapsed:.1f}s"
    assert live.cancel_all() == 0, "the dropped child must not be tracked"
    _assert_frozen(counter)


def test_kill_process_tree_never_raises_on_a_dead_pid():
    kill_process_tree(999_999_999)  # never raises by contract
