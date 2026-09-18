"""Create and remove the linked git worktrees a worktree session runs in.

Phase 1A taught the core to RUN a session inside a linked worktree; this module
is the missing half — the mechanism that CREATES and REMOVES one, plus the
naming rules around it. It is the ONLY copy of that mechanism: the desktop calls
it through the transient ``python -m cluxmate worktree … --json`` CLI and parses
the JSON, so the field names and error codes below are a frozen contract and
must not be renamed here without changing the TS side too.

Host-side by design: these commands run ``git`` directly, NOT through the
model's ``bash`` tool, the sandbox boundaries or the WriteFence — the same
precedent as ``core/checkpoints.py`` (the shadow repo). Nothing here is
reachable by the model, and nothing here writes ``~/.cluxmate/config.json``.

Payloads (exactly what the CLI prints with ``--json``)::

    info   {"ok":true,"cwd":…,"root":…,"config_root":…,"is_worktree":…,"branch":…}
    create {"ok":true,"name":…,"path":…,"branch":…,"base":<sha>,"base_ref":…,
            "project_root":…,"repo_root":…,"dirty":[…]}
    list   {"ok":true,"worktrees":[{"path":…,"name":…,"branch":…,"head":…,
                                    "is_main":…,"is_current":…}]}
    remove {"ok":true,"path":…,"branch":…,"branch_deleted":…,"pruned":…}
    error  {"ok":false,"error":<code>,"message":…,"details":{…}}

``WorktreeError.code`` is a closed set: ``not-a-repo``, ``bad-name``,
``in-worktree-container``, ``dirty`` (``details.files``), ``branch-exists``,
``base-missing``, ``not-found``, ``main-worktree``, ``git-failed``
(``details.stderr``) and ``usage``.

This module never prints: it either returns the ``ok: True`` payload or raises
``WorktreeError``, and the CLI is the thin layer that renders an error as the
error payload (stdout, one line) plus exit code 1.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

# `project_root` owns the project-root policy AND the git environment isolation
# (`_env`) for every git child CluxMate spawns. Both are borrowed here on
# purpose, so each policy has exactly one home (see `_run`).
from cluxmate.core import project_root

# One git call must never outlive this. `worktree add` on a large repository is
# the slowest thing here, and "slow" has to become an error, never a hang: the
# desktop waits on this CLI. The post-kill cleanup after a timeout is
# `project_root._reap` — the ONE copy of that measured Windows hazard (see
# `_run`), borrowed here rather than duplicated.
_TIMEOUT_SECONDS = 30

# Worktrees live in ONE container under the main worktree root, so a repository
# has exactly one directory to ignore (`<git-common-dir>/info/exclude`) and the
# desktop can tell a session worktree from a tree the user made by hand.
_CONTAINER = ".worktrees"
_BRANCH_PREFIX = "cluxmate/"

# Slug budget: a branch name of `cluxmate/<slug>` must stay readable in a git
# log and in the desktop's session list.
_SLUG_MAX_CHARS = 40

# `me`, then `me-2` … `me-21`: 20 attempts at a free suffix before giving up.
_MAX_SLUG_SUFFIXES = 20

# A dirty-tree report is a hint for the user, not an inventory.
_DIRTY_FILES_MAX = 20

_UNSAFE = re.compile(r"[^a-z0-9]+")


class WorktreeError(Exception):
    """A failure the CLI renders as ``{"ok":false,"error":code,…}``.

    ``code`` is one of the frozen codes, ``message`` is a single human sentence
    and ``details`` is free-form (``files`` for `dirty`, ``stderr`` for
    `git-failed`); it defaults to an empty dict so callers can always read it.
    """

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: dict = dict(details or {})


def slugify(text: str) -> str:
    """Normalize a name/title into a worktree slug, or ``""`` when none survives.

    lowercase → every run of non-``[a-z0-9]`` collapses to one ``-`` → strip the
    ends → cut to 40 characters (and strip again, so a cut never leaves a
    trailing dash). A CJK-only title normalizes to nothing at all: there is no
    transliteration anywhere in this codebase, so `create` reports ``bad-name``
    and the caller passes an explicit ``--name`` (the desktop dialog asks for
    one).
    """
    slug = _UNSAFE.sub("-", (text or "").lower()).strip("-")
    return slug[:_SLUG_MAX_CHARS].strip("-")


def worktree_container(root) -> Path:
    """``<repo_root>/.worktrees`` — where every worktree this module creates lives."""
    return Path(root) / _CONTAINER


def info(cwd: str) -> dict:
    """Describe a directory: the project-root policy, straight from its one home.

    ``config_root`` (not ``root``) is what a session in this directory reads its
    project config from, and it is what the desktop stores; inside a linked
    worktree it is the main worktree, everywhere else it is the cwd itself.
    """
    resolved = project_root.resolve(cwd)
    return {
        "ok": True,
        "cwd": resolved.cwd,
        "root": resolved.root,
        "config_root": resolved.config_root,
        "is_worktree": resolved.is_worktree,
        "branch": resolved.branch,
    }


def create(
    cwd: str,
    *,
    name: str | None = None,
    title: str | None = None,
    base: str | None = None,
    branch: str | None = None,
    allow_dirty: bool = False,
) -> dict:
    """Create a linked worktree under ``<repo_root>/.worktrees/<slug>``.

    The repository is resolved from ``cwd`` — so a worktree created from inside
    another worktree still lands in the MAIN repository's container — and the
    new tree starts on ``cluxmate/<slug>`` at ``--base`` (default ``HEAD``),
    with ``<git-common-dir>/info/exclude`` extended so the main tree keeps
    calling itself clean. Uncommitted changes in the main tree are refused
    unless ``allow_dirty``.
    """
    repo_root = _repo_root(cwd)
    _reject_container(cwd, repo_root)

    # `--name` > `--title` > "wt". An explicitly empty `--name` is taken at its
    # word (→ `bad-name`), not silently replaced by the title or the fallback.
    source = name if name is not None else (title or "wt")
    slug = slugify(source)
    if not slug:
        raise WorktreeError(
            "bad-name", f"'{source}' has no usable characters for a worktree name"
        )

    # An explicit branch is a reference the caller chose, so an existing one is
    # an error; a NAME is a label we own, so it is suffixed instead.
    explicit_branch = (branch or "").strip() or None
    if explicit_branch and _branch_exists(repo_root, explicit_branch):
        raise WorktreeError(
            "branch-exists",
            f"branch '{explicit_branch}' already exists",
            {"branch": explicit_branch},
        )

    final_slug, target = _free_slug(repo_root, slug)
    final_branch = explicit_branch or f"{_BRANCH_PREFIX}{final_slug}"
    base_sha, base_ref = _resolve_base(repo_root, base)

    dirty = _dirty_files(repo_root)
    if dirty and not allow_dirty:
        raise WorktreeError(
            "dirty",
            "the main worktree has uncommitted changes",
            {"files": dirty},
        )

    code, _, err = _run(
        ["worktree", "add", "-b", final_branch, str(target), base_sha],
        repo_root,
        # A checkout applies the user's EOL conversion and smudge filters
        # (git-lfs lives in the global config) — see `_env`.
        user_config=True,
    )
    if code != 0:
        raise WorktreeError(
            "git-failed",
            f"git worktree add failed: {_first_line(err)}",
            {"stderr": err.strip()},
        )

    _ensure_excluded(repo_root)
    return {
        "ok": True,
        "name": final_slug,
        "path": str(target),
        "branch": final_branch,
        "base": base_sha,
        "base_ref": base_ref,
        # `project_root` is the name the desktop reads — it is what a session in
        # this tree takes as its project root; `repo_root` is git's name for the
        # same directory.
        "project_root": repo_root,
        "repo_root": repo_root,
        "dirty": dirty,
    }


def list_worktrees(cwd: str) -> list[dict]:
    """Every worktree git knows in this repository, main worktree first.

    ``name`` is the container-relative name for a tree this module created and
    ``""`` for anything else (the main tree, or a worktree made by hand outside
    ``<repo>/.worktrees``); ``is_current`` compares against the resolved session
    cwd, not the repo root.
    """
    repo_root = _repo_root(cwd)
    current = _key(project_root.resolve(cwd).cwd)
    container = _key(worktree_container(repo_root))
    code, out, err = _run(["worktree", "list", "--porcelain"], repo_root)
    if code != 0:
        raise WorktreeError(
            "git-failed",
            f"git worktree list failed: {_first_line(err)}",
            {"stderr": err.strip()},
        )
    rows: list[dict] = []
    for block in _porcelain_blocks(out):
        path = block.get("worktree")
        if not path:
            continue
        ref = block.get("branch", "")
        branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ""
        key = _key(path)
        path = os.path.normpath(path)
        rows.append({
            "path": path,
            "name": os.path.basename(path) if _inside(key, container) else "",
            "branch": branch,
            "head": block.get("HEAD", ""),
            "is_main": key == _key(repo_root),
            "is_current": key == current,
        })
    return rows


def remove(
    cwd: str, target: str, *, force: bool = False, keep_branch: bool = False
) -> dict:
    """Remove a worktree (by container name or by path) and, normally, its branch.

    The tree must be one this repository registered under ``<repo>/.worktrees``:
    the main worktree is refused outright and anything outside the container is
    ``not-found``. A dirty tree needs ``force``; a directory that was deleted by
    hand while git still held its admin entry is ``git worktree prune``-d and
    reported as ``pruned: true``. A branch that cannot be deleted (checked out
    somewhere else) is NOT a failure — the tree is gone, and the payload says
    ``branch_deleted: false`` plus why.
    """
    repo_root = _repo_root(cwd)
    container = worktree_container(repo_root)
    path = _target_path(cwd, container, target)
    if _key(path) == _key(repo_root):
        raise WorktreeError("main-worktree", "the main worktree cannot be removed")
    if not _inside(_key(path), _key(container)):
        raise WorktreeError(
            "not-found",
            f"'{target}' is not a worktree of {repo_root}",
            {"container": str(container)},
        )
    row = {_key(r["path"]): r for r in list_worktrees(repo_root)}.get(_key(path))
    if row is None:
        raise WorktreeError("not-found", f"no worktree registered at {path}")

    pruned = False
    if not os.path.isdir(path):
        # The directory is gone but git still holds its admin entry, and
        # `git worktree remove` cannot run on a missing path — prune it instead.
        code, _, err = _run(["worktree", "prune"], repo_root)
        if code != 0:
            raise WorktreeError(
                "git-failed",
                f"git worktree prune failed: {_first_line(err)}",
                {"stderr": err.strip()},
            )
        pruned = True
    else:
        dirty = _dirty_files(path)
        if dirty and not force:
            raise WorktreeError(
                "dirty",
                "the worktree has uncommitted changes",
                {"files": dirty},
            )
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(path))
        # git re-checks dirtiness itself before deleting, so it must judge that
        # with the same rules our guard above used (`_env`).
        code, _, err = _run(args, repo_root, user_config=True)
        if code != 0:
            raise WorktreeError(
                "git-failed",
                f"git worktree remove failed: {_first_line(err)}",
                {"stderr": err.strip()},
            )

    branch = row["branch"]
    branch_deleted = False
    message = None
    if branch and not keep_branch:
        code, _, err = _run(["branch", "-D", branch], repo_root)
        branch_deleted = code == 0
        if not branch_deleted:
            message = (
                f"the worktree was removed but branch '{branch}' was kept: "
                f"{_first_line(err)}"
            )
    payload = {
        "ok": True,
        "path": str(path),
        "branch": branch,
        "branch_deleted": branch_deleted,
        "pruned": pruned,
    }
    if message:
        payload["message"] = message
    return payload


# --- git plumbing ----------------------------------------------------------


def _run(args: list[str], cwd: str, *, user_config: bool = False) -> tuple[int, str, str]:
    """Run ONE git command → ``(returncode, stdout, stderr)``. Never blocks.

    Mirrors ``project_root._run_git`` and borrows both its ``_env()`` and its
    ``_reap()`` rather than re-implementing either, because both exist for
    reasons that apply verbatim to every git child this module spawns:

    * ``stdin`` MUST be detached. Under ``agent stdio``, or when the desktop
      runs this CLI from its bridge, this process's stdin is a pipe; an MSYS2
      binary that inherits it blocks at STARTUP — ``git.exe`` would hang before
      it even parses argv, which looks like a frozen app rather than an error.
    * The timeout must be enforced by US. ``subprocess.run(timeout=…)`` on
      Windows kills the child and then calls ``communicate()`` AGAIN with no
      timeout (CPython ``subprocess.py``), joining the reader threads unbounded,
      so a child whose pipe write end is still held open hangs forever instead
      of raising ``TimeoutExpired``. Hence ``kill()`` → ``wait`` → close, which
      is exactly ``project_root._reap``; calling it (never a second
      ``communicate()``) is what keeps the cleanup itself bounded.
    * ``project_root._env()`` is the ONE home of the git environment isolation
      (``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_SYSTEM`` → ``os.devnull``,
      ``GIT_TERMINAL_PROMPT=0``, ``GIT_OPTIONAL_LOCKS=0``, and popping
      ``GIT_DIR``/``GIT_WORK_TREE``/``GIT_COMMON_DIR``). An inherited ``GIT_DIR``
      would make these commands act on a different repository — here that would
      create a worktree in the wrong tree — and a credential prompt or an
      optional lock wait would stall a CLI the desktop is waiting on.

    ``user_config=True`` (see ``_env``) hands the two config overrides back for
    the calls that are about CONTENT rather than identity or location.
    """
    git = shutil.which("git")
    if git is None:
        raise WorktreeError("not-a-repo", "git was not found on PATH")
    try:
        proc = subprocess.Popen(
            [git, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            env=_env(user_config=user_config),
        )
    except (OSError, ValueError) as exc:
        raise WorktreeError(
            "git-failed", f"could not run git: {exc}", {"stderr": str(exc)}
        ) from exc
    try:
        stdout, stderr = proc.communicate(timeout=_TIMEOUT_SECONDS)
    except (subprocess.SubprocessError, OSError, ValueError):
        # Delegated on purpose: `project_root._reap` is the ONE copy of the
        # measured Windows hazard, so a fix to it can never leave this module's
        # git calls able to hang the CLI the desktop is waiting on.
        project_root._reap(proc)
        raise WorktreeError(
            "git-failed",
            f"git {args[0] if args else ''} did not finish within {_TIMEOUT_SECONDS}s",
        ) from None
    return (
        proc.returncode,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def _env(*, user_config: bool = False) -> dict[str, str]:
    """``project_root._env()`` — plus an escape hatch for CONTENT questions.

    The isolation is right for every question about *identity or location* (which
    repo, which branch, which ref), and wrong for the two that are about the
    CONTENT of a working tree, because the conversion rules that decide what
    "modified" means live in exactly the config files ``_env`` nulls:

    Measured on this machine (Windows): ``C:/Program Files/Git/etc/gitconfig``
    sets ``core.autocrlf=true``. In a freshly committed repository whose
    checkout is CRLF — Git for Windows' default — the index holds LF, so with
    the system config nulled ``git status --porcelain`` reports every text file
    as `` M``. A ``create`` guard that calls a CLEAN repository dirty would
    refuse the feature everywhere on Windows, and ``remove`` would need
    ``--force`` for the same reason. The same nulling would also make
    ``git worktree add`` skip the user's smudge filters — ``filter.lfs.*`` is
    normally in the user's GLOBAL config, so an LFS repository would get pointer
    files checked out instead of their content.

    ``user_config=True`` therefore only un-nulls ``GIT_CONFIG_GLOBAL`` and
    ``GIT_CONFIG_SYSTEM`` (restoring whatever the parent process had, normally
    nothing = git's own defaults + the real files). Every other safety property
    of ``_env`` is retained: no inherited ``GIT_DIR``/``GIT_WORK_TREE``/
    ``GIT_COMMON_DIR``, no terminal prompt, no optional lock.
    """
    env = project_root._env()
    if user_config:
        env.pop("GIT_CONFIG_GLOBAL", None)
        env.pop("GIT_CONFIG_SYSTEM", None)
    return env


def _repo_root(cwd: str) -> str:
    """The git MAIN worktree root of ``cwd``, or ``not-a-repo``.

    ``project_root.resolve(cwd).root`` — deliberately ``.root``: it is the main
    worktree root for ANY directory inside a repository, including a linked
    worktree (where a new tree must still land in the main repository) and a
    plain-repo subdirectory (where ``.config_root`` deliberately does NOT
    redirect). ``resolve`` never raises, so the repo-ness of the answer is
    confirmed with one cheap ``git rev-parse --git-dir`` probe instead; a bare
    repository or an unborn HEAD is out of scope and simply fails that probe or
    the base resolution below.
    """
    root = project_root.resolve(cwd).root
    if not os.path.isdir(root):
        raise WorktreeError("not-a-repo", f"{cwd} is not a directory")
    if shutil.which("git") is None:
        raise WorktreeError("not-a-repo", "git was not found on PATH")
    code, _, _ = _run(["rev-parse", "--git-dir"], root)
    if code != 0:
        raise WorktreeError("not-a-repo", f"{cwd} is not inside a git repository")
    return root


def _reject_container(cwd: str, repo_root: str) -> None:
    """`<repo>/.worktrees` is a container, not a place to create a tree from."""
    if _key(cwd) == _key(worktree_container(repo_root)):
        raise WorktreeError(
            "in-worktree-container",
            f"{cwd} is the worktree container, not a repository root",
        )


def _free_slug(repo_root: str, slug: str) -> tuple[str, Path]:
    """The first free slug (``me``, ``me-2`` …) and the target path it may take.

    Auto-suffixing applies to an explicit ``--name`` as well (predictability
    over erroring): a name is a label we own, and rewriting it is friendlier
    than refusing a create the user already spelled out. Only a caller-chosen
    ``--branch`` that exists is an error, because a branch is a reference.
    """
    container = worktree_container(repo_root)
    for index in range(_MAX_SLUG_SUFFIXES + 1):
        candidate = slug if index == 0 else f"{slug}-{index + 1}"
        target = container / candidate
        if not target.exists() and not _branch_exists(
            repo_root, f"{_BRANCH_PREFIX}{candidate}"
        ):
            return candidate, target
    raise WorktreeError(
        "bad-name",
        f"no free worktree name for '{slug}' after {_MAX_SLUG_SUFFIXES} attempts",
    )


def _resolve_base(repo_root: str, base: str | None) -> tuple[str, str]:
    """The commit to branch from and the ref to report: ``--base`` > ``HEAD``.

    Resolved in the MAIN worktree and pinned to a 40-char sha, so the tree is
    created at the commit that was validated (and the payload can report it)
    even if a concurrent process moves the ref.
    """
    asked = (base or "").strip() or "HEAD"
    code, out, _ = _run(
        ["rev-parse", "--verify", "--quiet", f"{asked}^{{commit}}"], repo_root
    )
    sha = out.strip()
    if code != 0 or not sha:
        raise WorktreeError(
            "base-missing", f"'{asked}' is not a commit in this repository",
            {"base": asked},
        )
    base_ref = asked
    if asked == "HEAD":
        # Report the branch HEAD is on — that is what "based on master" means to
        # the user; a detached HEAD reports the sha's own "HEAD".
        code, out, _ = _run(["rev-parse", "--abbrev-ref", "HEAD"], repo_root)
        if code == 0 and out.strip():
            base_ref = out.strip()
    return sha, base_ref


def _branch_exists(repo_root: str, branch: str) -> bool:
    code, _, _ = _run(
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], repo_root
    )
    return code == 0


def _dirty_files(cwd: str) -> list[str]:
    """Uncommitted paths of a working tree, capped for display.

    Paths under ``<root>/.worktrees/`` are left out: that container is
    CluxMate's own, not the user's work. ``_ensure_excluded`` is best-effort, so
    on a repository whose ``<git-common-dir>/info`` is not writable the trees we
    created keep showing up as one untracked ``.worktrees/`` entry — and
    counting it would make the NEXT ``create`` refuse a tree the user never
    touched, blaming them for a directory we made. The entry is relative to the
    tree being asked about, which is why the test is on ``_CONTAINER`` and not
    on the container's absolute path (this is also the guard ``remove`` runs
    inside the worktree itself, where the container is not under the root at
    all). A tree whose only entries are ours is clean.
    """
    code, out, err = _run(["status", "--porcelain"], cwd, user_config=True)
    if code != 0:
        raise WorktreeError(
            "git-failed",
            f"git status failed: {_first_line(err)}",
            {"stderr": err.strip()},
        )
    prefix = _CONTAINER + "/"
    files: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        # `status --porcelain` writes the path relative to the tree it is asked
        # about, with either separator, so both are folded before the test.
        path = line[3:]
        if path.replace("\\", "/").startswith(prefix):
            continue
        files.append(path)
    return files[:_DIRTY_FILES_MAX]


def _common_dir(root: str) -> str | None:
    """git's COMMON dir as an absolute path, or None.

    Same ordered fallback chain as ``project_root._common_dir``: the absolute
    form needs git >= 2.31, and the plain form prints a path relative to the
    process cwd, which we resolve against ``root``.
    """
    for args in (
        ("--path-format=absolute", "--git-common-dir"),
        ("--git-common-dir",),
    ):
        code, out, _ = _run(["rev-parse", *args], root)
        if code != 0 or not out.strip():
            continue
        candidate = Path(out.strip())
        if not candidate.is_absolute():
            candidate = Path(root) / candidate
        return os.path.normpath(str(candidate))
    return None


def _ensure_excluded(root: str) -> None:
    """Make the main tree ignore ``.worktrees/``, idempotently and best-effort.

    ``<git-common-dir>/info/exclude`` rather than the user's TRACKED
    ``.gitignore``: ``git add -A`` obeys the exclude too, it needs no commit and
    it leaves no diff noise in a repository CluxMate does not own. Skipped when
    the repository already ignores the path (``git check-ignore``), and skipped
    again when our line is already there, so creating the tenth worktree does
    not append a tenth line.

    A write failure is swallowed: the tree already exists at this point, and
    failing the create over an unwritable ``info/`` would report a worktree that
    is in fact there. The cost is that the new tree keeps showing up as
    untracked in the main tree until the exclude can be written.
    """
    # "Already ignored?" is a config question (a user's core.excludesFile counts),
    # so it is asked with the user's own config in place.
    code, _, _ = _run(
        ["check-ignore", "--no-index", "-q", _CONTAINER], root, user_config=True
    )
    if code == 0:
        return
    common = _common_dir(root)
    if common is None:
        return
    exclude = Path(common) / "info" / "exclude"
    try:
        existing = exclude.read_text(encoding="utf-8", errors="replace")
    except OSError:
        existing = ""
    if any(line.strip() == f"{_CONTAINER}/" for line in existing.splitlines()):
        return
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write(f"{_CONTAINER}/\n")
    except OSError:
        return


def _porcelain_blocks(porcelain: str) -> list[dict[str, str]]:
    """``git worktree list --porcelain`` → one dict per blank-line-separated block."""
    blocks: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in porcelain.splitlines():
        if not line.strip():
            if current:
                blocks.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        blocks.append(current)
    return blocks


def _target_path(cwd: str, container: Path, target: str) -> str:
    """Resolve a ``remove`` target: a bare NAME lives in the container, else a PATH.

    The presence of a separator is the test: ``me`` is a name, while ``./me``,
    ``../elsewhere`` and an absolute path are paths, resolved against the
    session cwd so a relative path means what the caller sees — not what this
    process's cwd happens to be.
    """
    raw = (target or "").strip()
    if not raw:
        raise WorktreeError("usage", "remove needs a worktree name or a path")
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    if "/" in raw or "\\" in raw:
        return os.path.normpath(os.path.join(cwd, raw))
    return os.path.normpath(str(container / raw))


def _inside(key: str, container_key: str) -> bool:
    """True when an already-``_key``-ed path is strictly inside the container."""
    return key.startswith(container_key.rstrip(os.sep) + os.sep)


def _key(path) -> str:
    """One comparison key for a path: absolute, symlink-free, case-folded.

    ``git worktree list`` echoes the path as git stored it, while a caller hands
    us their own spelling of the same place — plain string equality is not
    enough on Windows (``C:\\Repo`` vs ``c:\\repo``) or behind a symlinked temp
    directory.
    """
    return os.path.normcase(os.path.realpath(str(path)))


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return "git failed"
