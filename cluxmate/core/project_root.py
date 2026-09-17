"""Resolve the PROJECT ROOT of a working directory.

CluxMate equates a project with a session's cwd everywhere. A git worktree
breaks that equation: the session's tree is ``<repo>/.worktrees/<slug>`` while
the project's config state (trust, permissions, skills, mcp, hooks, agents,
AGENTS.md, retrieval facts) lives in the MAIN worktree. So config readers take
the CONFIG ROOT (``ProjectRoot.config_root``), while everything that means "the
tree I may write" keeps the session cwd.

THE POLICY, stated once, in ``config_root``: the config root is the session cwd
EXCEPT when the session runs inside a LINKED git worktree, where it is the main
worktree root. That exception is the whole point of this module — only a linked
worktree (working tree ``<repo>/.worktrees/<slug>``, own branch) is redirected to
the main worktree's config. Everywhere else, including a SUBDIRECTORY of an
ordinary repo (``<repo>/pkg``), the session keeps its own directory, so
``<repo>/pkg/.cluxmate/`` is still what is read — identical to the behaviour
before worktree support existed. ``root`` stays informative: it is the git main
worktree root for ANY directory inside a repo, and callers that read project
config must take ``config_root`` instead of it.

The main worktree root is the PARENT of git's *common* dir. Do not use
``--show-toplevel``: inside a linked worktree it returns the worktree itself.

``is_worktree`` is True iff the session cwd lives inside a LINKED worktree,
i.e. git's dir for that cwd differs from git's *common* dir. So: a non-git dir,
a plain repo and anything inside it are False (there git's dir *is* the common
dir); the root of a linked worktree and anything inside it are True.

Never raises: no git / not a repo / timeout all degrade to ``root == cwd``,
which is exactly today's behaviour.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_TIMEOUT_SECONDS = 5

# Bound on the post-kill cleanup of a probe that timed out (see _reap). Kept
# separate from _TIMEOUT_SECONDS: cleanup is not a second chance for the probe
# to answer, it is how we let go of it without waiting on it again.
_REAP_TIMEOUT_SECONDS = 1

# Ordered fallbacks: the absolute form needs git >= 2.31; the plain form prints
# a path relative to the process cwd, which we resolve against the session cwd.
_COMMON_DIR_ARG_SETS: tuple[tuple[str, ...], ...] = (
    ("--path-format=absolute", "--git-common-dir"),
    ("--git-common-dir",),
)

# Same ordered fallback for the git dir *of the session cwd*: inside a linked
# worktree it is ``<common>/worktrees/<slug>``, everywhere else it is the common
# dir itself. Both forms print a cwd-relative path in the plain case.
_GIT_DIR_ARG_SETS: tuple[tuple[str, ...], ...] = (
    ("--path-format=absolute", "--git-dir"),
    ("--git-dir",),
)

_CACHE: dict[str, "ProjectRoot"] = {}


@dataclass(frozen=True)
class ProjectRoot:
    cwd: str
    root: str
    is_worktree: bool
    branch: str | None

    @property
    def config_root(self) -> str:
        """THE directory project-config readers must take.

        The session cwd, except inside a linked worktree where it is the main
        worktree root (see the module docstring). A plain-repo subdirectory
        session therefore keeps its own directory exactly as before this
        module existed; only linked worktrees are redirected.
        """
        return self.root if self.is_worktree else self.cwd


def clear_cache() -> None:
    """Drop the process-level cache (tests, and a cwd that just became a repo)."""
    _CACHE.clear()


def resolve(cwd: str, *, use_cache: bool = True) -> ProjectRoot:
    try:
        key = str(Path(cwd).resolve())
    except (OSError, ValueError):
        # ValueError: a path with an embedded NUL cannot be `stat`ed on Windows
        # (and `subprocess` rejects it too) — the raw string is all we have.
        key = str(cwd)
    if use_cache:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached
    try:
        info = _probe(key)
    except (OSError, ValueError):
        # Same unresolvable path: git can be neither run nor probed from it, so
        # degrade to the cwd exactly like "no git / not a repo" above. This
        # function is documented never to raise.
        info = ProjectRoot(key, key, False, None)
    if use_cache:
        _CACHE[key] = info
    return info


def _env() -> dict[str, str]:
    env = os.environ.copy()
    # `git -C <cwd>` does NOT override these: the environment wins over
    # repository discovery, so an inherited value would make `resolve()`
    # describe a different repo — and trust/permissions/hooks are keyed on
    # its answer. Only the CONFIG isolation is ours to set.
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
        env.pop(key, None)
    # Isolate from the user's global/system git config, like checkpoints._env.
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _run_git(git: str, cwd: str, *args: str) -> str | None:
    """Run ONE git probe, or None. Never raises — and never blocks.

    Two Windows hazards, both of which STALL the JSON-RPC handshake rather
    than fail it, which is why this is not a plain ``subprocess.run``:

    * ``stdin`` MUST be detached. Under ``agent stdio`` this process's stdin is
      the JSON-RPC pipe; an MSYS2 binary that inherits it blocks at startup
      until the pipe closes — never, for a live desktop bridge. ``tools/bash.py``
      detaches it for exactly this reason (Git Bash's ``bash.exe``), and
      ``git.exe`` is the same runtime: with it attached ``initialize`` never
      answers, so the desktop's ``ensureBridge`` promise never settles and a
      sent message produces no result at all. ``checkpoints._run`` detaches it
      too.
    * The timeout must be enforced by US. ``subprocess.run(timeout=…)`` on
      Windows kills the child and then calls ``communicate()`` AGAIN with no
      timeout (CPython ``subprocess.py``:553-559); that second call joins the
      reader threads unbounded, so a child whose pipe write end is still held
      open hangs forever instead of raising ``TimeoutExpired``.

    A probe that cannot answer degrades exactly like a missing git: ``None``,
    and the caller falls back to ``root == cwd``.
    """
    try:
        proc = subprocess.Popen(
            [git, "-C", cwd, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=_env(),
        )
    except (OSError, ValueError):
        return None
    try:
        stdout, _ = proc.communicate(timeout=_TIMEOUT_SECONDS)
    except (subprocess.SubprocessError, OSError, ValueError):
        _reap(proc)
        return None
    if proc.returncode != 0:
        return None
    return stdout.decode("utf-8", errors="replace").strip()


def _reap(proc: subprocess.Popen) -> None:
    """Best-effort disposal of a probe that outlived its timeout.

    Deliberately does NOT ``communicate()`` again — that is the unbounded join
    described in ``_run_git``. Closing our pipe ends is what releases the
    orphaned reader threads, and both steps are bounded, so the cleanup cannot
    become a second hang of its own.
    """
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=_REAP_TIMEOUT_SECONDS)
    except Exception:
        pass
    for stream in (proc.stdout, proc.stderr):
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass


def _rev_parse_path(git: str, cwd: str, arg_sets: tuple[tuple[str, ...], ...]) -> str | None:
    """First ``rev-parse`` arg set that succeeds, as a clean absolute path."""
    for args in arg_sets:
        out = _run_git(git, cwd, "rev-parse", *args)
        if not out:
            continue
        candidate = Path(out)
        if not candidate.is_absolute():
            candidate = Path(cwd) / candidate
        # The plain form resolves against the cwd and can hand back paths like
        # ``<repo>/pkg/../.git``; collapse the ``..`` so one location always
        # yields one string (the parent of that string is the project root).
        return os.path.normpath(str(candidate))
    return None


def _common_dir(git: str, cwd: str) -> str | None:
    return _rev_parse_path(git, cwd, _COMMON_DIR_ARG_SETS)


def _git_dir(git: str, cwd: str) -> str | None:
    return _rev_parse_path(git, cwd, _GIT_DIR_ARG_SETS)


def _branch(git: str, cwd: str) -> str | None:
    out = _run_git(git, cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if not out or out == "HEAD":  # detached
        return None
    return out


def _probe(cwd: str) -> ProjectRoot:
    git = shutil.which("git")
    if git is None:
        return ProjectRoot(cwd, cwd, False, None)
    common = _common_dir(git, cwd)
    if common is None:
        return ProjectRoot(cwd, cwd, False, None)
    if os.path.normcase(common) == os.path.normcase(cwd):
        # A bare repo passed as the cwd: it is its own root, not a worktree.
        return ProjectRoot(cwd, cwd, False, _branch(git, cwd))
    # A linked worktree is exactly the case where git's dir for this cwd is not
    # the common dir; in a plain repo (any depth) the two are the same path, and
    # an unresolvable git dir stays False rather than guessing.
    git_dir = _git_dir(git, cwd)
    is_worktree = git_dir is not None and os.path.normcase(git_dir) != os.path.normcase(common)
    return ProjectRoot(cwd, str(Path(common).parent), is_worktree, _branch(git, cwd))
