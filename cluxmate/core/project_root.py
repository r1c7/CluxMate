"""Resolve the PROJECT ROOT of a working directory.

CluxMate equates a project with a session's cwd everywhere. A git worktree
breaks that equation: the session's tree is ``<repo>/.worktrees/<slug>`` while
the project's config state (trust, permissions, skills, mcp, hooks, agents,
AGENTS.md, retrieval facts) lives in the MAIN worktree. So config readers take
the project root, while everything that means "the tree I may write" keeps the
session cwd.

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


def clear_cache() -> None:
    """Drop the process-level cache (tests, and a cwd that just became a repo)."""
    _CACHE.clear()


def resolve(cwd: str, *, use_cache: bool = True) -> ProjectRoot:
    try:
        key = str(Path(cwd).resolve())
    except OSError:
        key = str(cwd)
    if use_cache:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached
    info = _probe(key)
    if use_cache:
        _CACHE[key] = info
    return info


def _env() -> dict[str, str]:
    env = os.environ.copy()
    # Isolate from the user's global/system git config, like checkpoints._env.
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _run_git(git: str, cwd: str, *args: str) -> str | None:
    try:
        r = subprocess.run(
            [git, "-C", cwd, *args], capture_output=True,
            timeout=_TIMEOUT_SECONDS, env=_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", errors="replace").strip()


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
