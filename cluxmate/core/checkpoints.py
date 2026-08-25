"""CheckpointManager — shadow-git workspace snapshots for undo/rewind.

The agent can modify files through several tools (search_replace and, more
broadly, bash: `echo >`, `sed`, `rm`, `mv`). Only a *workspace snapshot*
captures every change regardless of how it was made, so checkpoints are backed
by a **shadow git repository** that is completely independent of the user's own
`.git`:

- The shadow repo lives at ``~/.cluxmate/checkpoints/<sha1(cwd)>.git`` (one
  repo per working directory, shared across sessions in that directory).
- All git invocations set ``GIT_DIR`` / ``GIT_WORK_TREE`` via the environment
  and disable global/system config, so the user's real repository history and
  index are never touched.
- The shadow repo's ``info/exclude`` lists ``.git/`` (the user's real repo) and
  ``.cluxmate/`` (CluxMate's own per-project state — permissions.json,
  mcp.json, skills — written by UI toggles/housekeeping, not the agent's turn)
  so neither is ever sucked into a snapshot or reverted by undo.

If git is not on PATH the whole feature degrades to a no-op: ``available()``
returns False and every method returns an empty/neutral result so the agent
keeps working without checkpoints.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# git's canonical empty-tree object — used as the "parent" when diffing the
# very first commit (which has no real parent).
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# Skip embedding content for files larger than this in a diff payload.
_MAX_DIFF_BYTES = 256 * 1024

# Snapshot size cap: files larger than this are never staged. Incompressible
# binaries (models, archives, media) defeat git's delta compression, so a single
# stray asset can bloat a shadow repo into the gigabytes (observed: a ~1 GB ASR
# model pack grew one repo to 4.3 GB).
_MAX_FILE_BYTES = 20 * 1024 * 1024

# A shadow repo's history is capped at this many commits; older checkpoints are
# evicted via a shallow boundary so an active project's timeline can't grow
# without bound.
_MAX_COMMITS = 100

# Shadow repos whose latest snapshot is older than this many days are evicted.
_RETENTION_DAYS = 30


class CheckpointManager:
    """Shadow-git snapshots of a working directory."""

    def __init__(self, cwd: str):
        self._cwd = str(Path(cwd).resolve())
        digest = hashlib.sha1(self._cwd.encode("utf-8")).hexdigest()
        self._shadow_dir = str(Path.home() / ".cluxmate" / "checkpoints" / f"{digest}.git")
        self._git = shutil.which("git")
        self._initialized = False

    # -- environment / process helpers -------------------------------------

    def available(self) -> bool:
        """True when git is on PATH. When False the feature is a no-op."""
        return self._git is not None

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["GIT_DIR"] = self._shadow_dir
        env["GIT_WORK_TREE"] = self._cwd
        # Isolate from the user's global/system git config (aliases, hooks,
        # signing, identity) so snapshots are deterministic and side-effect free.
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    @staticmethod
    def _env_for(shadow_dir: str) -> dict[str, str]:
        """Env pointing at an arbitrary shadow repo (no work-tree) — used by
        retention/eviction, which walk OTHER projects' repos under the
        checkpoints root rather than this instance's own."""
        env = os.environ.copy()
        env["GIT_DIR"] = shadow_dir
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    def _run(
        self, *args: str, check: bool = False, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess:
        """Run a git command against the shadow repo.

        stdin is set to DEVNULL so git can never block waiting on input (e.g. a
        credential or editor prompt), and a wall-clock timeout bounds any hang
        so a stuck git cannot stall the JSON-RPC handshake.
        """
        return subprocess.run(
            [self._git, *args],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            cwd=self._cwd,
            env=env or self._env(),
            check=check,
            timeout=30,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    # -- lifecycle ---------------------------------------------------------

    def ensure_init(self) -> bool:
        """Create and configure the shadow repo if needed. Idempotent.

        Returns True when the shadow repo is ready, False when git is
        unavailable (the feature is then a no-op).

        On first use per process, also runs the retention sweep: evicting
        stale/oversized sibling repos and capping this repo's history.
        """
        if not self.available():
            return False
        if self._initialized:
            return True
        try:
            ok = self._ensure_init_inner()
        except Exception:
            # Any git failure (timeout, permission, corrupt repo) disables
            # checkpoints for this process rather than breaking agent init.
            self._git = None
            return False
        if ok:
            try:
                self.run_retention()
            except Exception:
                pass  # cleanup must never take the feature down
        return ok

    def _ensure_init_inner(self) -> bool:
        git_dir = Path(self._shadow_dir)
        if not git_dir.exists():
            git_dir.parent.mkdir(parents=True, exist_ok=True)
            # A bare repo whose work-tree is the project directory. `git init`
            # refuses GIT_WORK_TREE in the environment, so init with a
            # worktree-free env; every later call supplies it via _env().
            init_env = os.environ.copy()
            init_env["GIT_CONFIG_GLOBAL"] = os.devnull
            init_env["GIT_CONFIG_SYSTEM"] = os.devnull
            self._run("init", "--bare", self._shadow_dir, check=True, env=init_env)
            # Pin an identity local to this shadow repo (global/system config
            # is disabled in _env, so commits would otherwise fail).
            self._run("config", "user.name", "CluxMate")
            self._run("config", "user.email", "checkpoints@cluxmate.local")
            self._run("config", "commit.gpgsign", "false")
            # The commit cap rewrites history via a shallow boundary; a reflog
            # would keep the dropped commits alive and defeat gc's reclamation.
            self._run("config", "core.logAllRefUpdates", "false")

        # Always ensure our exclude patterns are present:
        # - `.git/`       without it `git add -A` sucks the user's real repo
        #                 history into every snapshot.
        # - `.cluxmate/` CluxMate's own per-project state (permissions.json,
        #                 mcp.json, skills) is written by UI toggles/housekeeping,
        #                 not the agent's turn. Snapshotting it would pollute the
        #                 turn diff and let undo revert a permission/MCP change.
        # Append any missing pattern (idempotent — upgrades an existing shadow
        # repo that only had `.git/`).
        info_dir = git_dir / "info"
        info_dir.mkdir(parents=True, exist_ok=True)
        exclude = info_dir / "exclude"
        try:
            existing = exclude.read_text("utf-8") if exclude.exists() else ""
        except OSError:
            existing = ""
        lines = existing.splitlines()
        additions = [p for p in (".git/", ".cluxmate/") if p not in lines]
        if additions:
            prefix = existing if existing.endswith("\n") or existing == "" else existing + "\n"
            exclude.write_text(prefix + "\n".join(additions) + "\n", encoding="utf-8")

        self._initialized = True
        return True

    # -- retention ---------------------------------------------------------

    def run_retention(self) -> None:
        """Sweep the whole checkpoints root: evict stale sibling repos, cap this
        repo's history, and prune loose objects. Idempotent and safe — only this
        process's own repo is mutated for the commit cap; sibling repos are
        touched solely to decide (and perform) whole-repo eviction."""
        self._evict_stale_repos()
        self._cap_commits()
        self._gc()

    def _root(self) -> Path:
        """The directory holding all shadow repos (parent of this repo's dir)."""
        return Path(self._shadow_dir).parent

    @staticmethod
    def _rmtree(path: Path) -> None:
        """Delete a shadow repo even on Windows, where git writes its object
        files read-only (0o444) and shutil.rmtree chokes on them."""
        def _on_error(func, p, exc_info):
            # Clear the read-only bit and retry the failed operation once.
            try:
                os.chmod(p, stat.S_IWRITE)
                func(p)
            except OSError:
                pass
        shutil.rmtree(path, onerror=_on_error)

    def _evict_stale_repos(self) -> None:
        """Delete any shadow repo whose latest commit predates the retention
        window. The current process's own repo is always skipped — it is in
        active use and its size is bounded separately by the commit cap."""
        root = self._root()
        try:
            entries = list(root.iterdir()) if root.is_dir() else []
        except OSError:
            return
        deadline = time.time() - _RETENTION_DAYS * 86400
        own = Path(self._shadow_dir).resolve()
        for entry in entries:
            name = entry.name
            if name == "_gc_lock" or not entry.is_dir() or not name.endswith(".git"):
                continue
            if entry.resolve() == own:
                # This process's own repo is being used right now — never evict
                # it (its size is bounded separately by the commit cap).
                continue
            env = self._env_for(str(entry))
            latest = subprocess.run(
                [self._git, "log", "-1", "--format=%ct"],
                capture_output=True, stdin=subprocess.DEVNULL,
                env=env, timeout=30, text=True, errors="replace",
            )
            if latest.returncode != 0:
                # No commits yet (fresh repo) → keep it; it's not stale. We can't
                # distinguish that from a corrupt repo here, so err on the side
                # of keeping — a corrupt repo is harmless disk, a wrong delete is
                # lost undo history.
                continue
            try:
                last_ts = int(latest.stdout.strip())
            except ValueError:
                continue
            if last_ts < deadline:
                self._rmtree(entry)

    def _cap_commits(self) -> None:
        """Limit this repo's history to ``_MAX_COMMITS`` via a shallow boundary.

        A plain ref rewrite would leave the older commits' objects reachable and
        won't actually shorten `rev-list`. Writing the ``shallow`` file makes
        the boundary commit the true tip of a shortened history, so `rev-list` /
        `log` / `list()` all see only the newest N — and a later `gc` drops the
        unreachable older objects. Subsequent commits extend from the boundary,
        so the history stays capped as it grows.
        """
        r = self._run("rev-list", "--count", "HEAD")
        if r.returncode != 0:
            return
        try:
            count = int(r.stdout.strip())
        except ValueError:
            return
        if count <= _MAX_COMMITS:
            return
        # Newest-commits-first; --skip N --max-count 1 gives the (N+1)th newest,
        # i.e. the oldest commit to KEEP — everything before it is dropped.
        boundary = self._run("rev-list", "HEAD", "--skip", str(_MAX_COMMITS - 1), "--max-count", "1")
        if boundary.returncode != 0:
            return
        sha = boundary.stdout.strip()
        if not sha:
            return
        # Overwrite, not append: the newest boundary is always reached first
        # when walking from HEAD, so any earlier entry is a redundant ancestor
        # that would just accumulate over repeated caps.
        shallow_file = Path(self._shadow_dir) / "shallow"
        shallow_file.write_text(sha + "\n", encoding="utf-8")

    def _gc(self) -> None:
        """Prune objects no longer reachable (e.g. after the commit cap dropped
        old commits). Skipped when another process holds the gc lock; a stale
        lock (crashed process) older than an hour is reclaimed."""
        root = self._root()
        lock = root / "_gc_lock"
        try:
            os.mkdir(lock)
        except FileExistsError:
            # Reclaim a lock left behind by a process that died mid-gc.
            try:
                stale = time.time() - lock.stat().st_mtime > 3600
            except OSError:
                return
            if not stale:
                return
            try:
                lock.rmdir()
                os.mkdir(lock)
            except OSError:
                return
        except OSError:
            return
        try:
            # Expire the reflog first: it would otherwise keep the commits the
            # shallow cap just dropped alive for 90 days, defeating reclamation.
            self._run("reflog", "expire", "--expire=now", "--all")
            self._run("gc", "--prune=now")
        except Exception:
            pass
        finally:
            try:
                lock.rmdir()
            except OSError:
                pass

    # -- snapshots ---------------------------------------------------------

    def _subject(self, session_id: str, label: str) -> str:
        """Pack commit metadata as ``<session_id>\\t<label>\\t<iso_ts>`` so list()
        and the session-scoped restore helpers can recover the owning session."""
        ts = datetime.now(timezone.utc).isoformat()
        # Tabs separate fields; strip any tab/newline from the label so the
        # first commit-message line stays parseable.
        safe_label = label.replace("\t", " ").replace("\n", " ").strip()
        return f"{session_id}\t{safe_label}\t{ts}"

    def snapshot(self, session_id: str, label: str) -> str | None:
        """Stage the whole work-tree and commit it. Returns the commit sha.

        Uses --allow-empty so a no-op turn still produces a checkpoint node,
        keeping the timeline consistent. The commit subject packs metadata as
        ``<session_id>\\t<label>\\t<iso_ts>`` so list() can filter by session.
        """
        if not self.ensure_init():
            return None
        if not self._add_staged():
            return None
        return self._commit(session_id, label)

    def _commit(self, session_id: str, label: str) -> str | None:
        """Commit whatever is staged and return the resulting sha (None on any
        git failure)."""
        commit = self._run("commit", "--allow-empty", "-m", self._subject(session_id, label))
        if commit.returncode != 0:
            return None
        head = self._run("rev-parse", "HEAD")
        if head.returncode != 0:
            return None
        return head.stdout.strip() or None

    def _add_staged(self) -> bool:
        """`git add -A` over the whole work-tree, skipping files over the size cap.

        Returns False when git reports a failure (or an oversized file made
        everything unstaged) — the snapshot should then be aborted rather than
        committed as a misleading empty node.

        The cap is enforced in-process, not via `.gitignore` / exclude, because
        those would also suppress `git status`-style delta detection; we want the
        file visible as an untracked path the model can read, just never committed
        as a blob.
        """
        add = self._run("add", "-A")
        if add.returncode != 0:
            return False
        oversized = self._oversized_staged()
        if not oversized:
            return True
        # `git reset -- <path>` is the inverse of `git add <path>`: it drops a
        # new file's index entry, and reverts an already-tracked file's index to
        # its HEAD version (so the commit keeps the last under-cap copy rather
        # than re-recording the oversized blob). Unlike `restore --staged`, it
        # needs no HEAD and so works on the very first commit. The work-tree file
        # is left untouched in both cases.
        reset = self._run("reset", "--", *sorted(oversized))
        return reset.returncode == 0

    def _oversized_staged(self) -> set[str]:
        """Paths currently in the index whose file exceeds ``_MAX_FILE_BYTES``.

        Uses the work-tree file size as a proxy for the blob size (the shadow
        repo runs with no global/system config and no clean filters, so for a
        regular file the two agree; a symlink is stored as its tiny target-path
        string, which ``lstat`` — not ``stat`` — measures, so a link to a giant
        file is not wrongly flagged). One ``ls-files`` subprocess + O(files)
        cheap stats keeps this fast on the per-turn hot path.
        """
        r = self._run("ls-files", "-z")
        if r.returncode != 0:
            return set()
        oversized: set[str] = set()
        for path in r.stdout.split("\x00"):
            if not path:
                continue
            try:
                if os.lstat(Path(self._cwd) / path).st_size > _MAX_FILE_BYTES:
                    oversized.add(path)
            except OSError:
                continue
        return oversized

    def _snapshot_paths(self, session_id: str, label: str, paths: set[str]) -> str | None:
        """Commit a snapshot that stages ONLY the given paths — never `add -A`.

        Used by restore() for its before/after-restore checkpoints. The work-tree
        is physically shared across sessions, so `add -A` would absorb other
        sessions' in-flight on-disk changes into this session's history (which a
        later rewind's `A..HEAD` would then treat as this session's own and
        revert). Staging just the paths this restore touches keeps those files'
        index blobs untouched, so they never enter our commit. A deleted path is
        staged too (git add records the removal). Empty `paths` still commits
        (--allow-empty) so the before/after anchors always exist.
        """
        if not self.ensure_init():
            return None
        if paths:
            # `git add -- <paths>` records both modifications and deletions for
            # the listed paths. Passing them explicitly (not -A) is what scopes
            # the stage to this session's files.
            add = self._run("add", "--", *sorted(paths))
            if add.returncode != 0:
                return None
            oversized = self._oversized_staged() & set(paths)
            if oversized:
                self._run("reset", "--", *sorted(oversized))
        commit = self._run("commit", "--allow-empty", "-m", self._subject(session_id, label))
        if commit.returncode != 0:
            return None
        head = self._run("rev-parse", "HEAD")
        if head.returncode != 0:
            return None
        return head.stdout.strip() or None

    # -- listing -----------------------------------------------------------

    def _files_changed(self, sha: str) -> int:
        """Count files changed by a commit relative to its parent."""
        parent = self._parent(sha)
        r = self._run("diff", "--name-only", parent, sha)
        if r.returncode != 0:
            return 0
        return len([ln for ln in r.stdout.splitlines() if ln.strip()])

    def _parent(self, sha: str) -> str:
        """Return the parent commit, or the empty-tree sha for a root commit."""
        r = self._run("rev-parse", "--verify", "--quiet", f"{sha}^")
        p = r.stdout.strip()
        return p if (r.returncode == 0 and p) else _EMPTY_TREE

    def _commit_session(self, sha: str) -> str:
        """The session_id a commit belongs to (first tab field of its subject)."""
        r = self._run("show", "-s", "--format=%s", sha)
        if r.returncode != 0:
            return ""
        return r.stdout.split("\t", 1)[0].strip()

    def _files_in_commit(self, sha: str) -> set[str]:
        """Paths a commit changed relative to its parent."""
        r = self._run("diff", "--no-renames", "--name-only", "-z", self._parent(sha), sha)
        if r.returncode != 0:
            return set()
        return {p for p in r.stdout.split("\x00") if p}

    def _commits_since(self, checkpoint_id: str) -> list[str]:
        """SHAs of commits reachable from HEAD but not from checkpoint_id
        (i.e. everything committed after the target checkpoint), oldest first."""
        r = self._run("rev-list", "--reverse", f"{checkpoint_id}..HEAD")
        if r.returncode != 0:
            return []
        return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]

    def _session_touched_since(self, checkpoint_id: str, session_id: str) -> set[str]:
        """Files changed by THIS session's own commits after checkpoint_id.

        This is the heart of session-scoped restore: the shadow repo is shared
        across all sessions in a working directory, so `diff checkpoint..HEAD`
        would include files touched by OTHER sessions interleaved in the shared
        history. We instead walk only the commits whose session_id matches and
        union the files they changed — so a rewind never reverts another
        session's work.
        """
        touched: set[str] = set()
        for sha in self._commits_since(checkpoint_id):
            if self._commit_session(sha) == session_id:
                touched |= self._files_in_commit(sha)
        return touched

    def _other_sessions_touched_since(self, checkpoint_id: str, session_id: str) -> set[str]:
        """Files changed by OTHER sessions' commits after checkpoint_id — used to
        flag genuine same-file conflicts a rewind would clobber."""
        touched: set[str] = set()
        for sha in self._commits_since(checkpoint_id):
            if self._commit_session(sha) != session_id:
                touched |= self._files_in_commit(sha)
        return touched

    def _uncommitted_paths(self, paths: set[str]) -> set[str]:
        """Subset of `paths` whose on-disk content differs from HEAD (the latest
        snapshot). These carry changes no snapshot has recorded yet — e.g. another
        session (or the user) edited the file mid-turn, before its own checkpoint
        was taken. A restore would overwrite them, so they count as conflicts even
        though no *commit* by another session exists yet."""
        if not paths:
            return set()
        # `git diff --name-only HEAD -- <paths>` reports work-tree vs HEAD drift,
        # including files deleted or created on disk since the last snapshot.
        r = self._run("diff", "--name-only", "-z", "HEAD", "--", *sorted(paths))
        if r.returncode != 0:
            return set()
        return {p for p in r.stdout.split("\x00") if p}

    def list(self, session_id: str) -> list[dict[str, Any]]:
        """Checkpoints for a session, newest first."""
        if not self.ensure_init():
            return []
        # NUL-separate fields; %ct is committer unix time. Guard against an
        # empty repo (no commits yet) where rev-parse HEAD fails.
        r = self._run("log", "--format=%H%x00%s%x00%ct")
        if r.returncode != 0:
            return []
        out: list[dict[str, Any]] = []
        for line in r.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("\x00")
            if len(parts) != 3:
                continue
            sha, subject, ctime = parts
            fields = subject.split("\t")
            sid = fields[0] if fields else ""
            if sid != session_id:
                continue
            label = fields[1] if len(fields) > 1 else ""
            iso = fields[2] if len(fields) > 2 else ""
            out.append({
                "id": sha,
                "label": label,
                "timestamp": iso or ctime,
                "files_changed": self._files_changed(sha),
            })
        return out

    # -- summary -----------------------------------------------------------

    def summary(self, checkpoint_id: str) -> list[dict[str, Any]]:
        """Per-file change summary a checkpoint introduced vs its parent.

        Lightweight companion to diff(): returns status + added/deleted line
        counts but NOT file contents, so it can be streamed inline after every
        turn without embedding blobs. Content is fetched lazily via diff() when
        the user clicks a file.
        """
        if not self.ensure_init():
            return []
        parent = self._parent(checkpoint_id)
        # --name-status gives A/M/D; --numstat gives add/del counts. Merge on
        # path. --no-renames so a rename shows as delete+add (simpler to render).
        status_by_path: dict[str, str] = {}
        st = self._run("diff", "--no-renames", "--name-status", "-z", parent, checkpoint_id)
        if st.returncode == 0:
            toks = [t for t in st.stdout.split("\x00") if t != ""]
            for i in range(0, len(toks) - 1, 2):
                status_by_path[toks[i + 1]] = toks[i][0]

        out: list[dict[str, Any]] = []
        ns = self._run("diff", "--no-renames", "--numstat", "-z", parent, checkpoint_id)
        if ns.returncode != 0:
            return []
        # numstat -z: "add\tdel\tpath\0" repeated. Binary files show "-\t-".
        toks = [t for t in ns.stdout.split("\x00") if t != ""]
        for tok in toks:
            parts = tok.split("\t")
            if len(parts) < 3:
                continue
            add_s, del_s, path = parts[0], parts[1], parts[2]
            try:
                additions = int(add_s)
                deletions = int(del_s)
            except ValueError:
                additions = deletions = 0  # binary
            out.append({
                "path": path,
                "status": status_by_path.get(path, "M"),
                "additions": additions,
                "deletions": deletions,
            })
        return out

    # -- diff --------------------------------------------------------------

    def _show(self, sha: str, path: str) -> str | None:
        """Content of a file at a commit, or None if absent/too large."""
        # size first, to skip embedding huge blobs
        size = self._run("cat-file", "-s", f"{sha}:{path}")
        if size.returncode == 0:
            try:
                if int(size.stdout.strip()) > _MAX_DIFF_BYTES:
                    return "[file too large to display]"
            except ValueError:
                pass
        r = self._run("show", f"{sha}:{path}")
        if r.returncode != 0:
            return None
        return r.stdout

    def diff(self, checkpoint_id: str) -> list[dict[str, Any]]:
        """Per-file changes a checkpoint introduced vs its parent."""
        if not self.ensure_init():
            return []
        parent = self._parent(checkpoint_id)
        r = self._run("diff", "--name-status", "-z", parent, checkpoint_id)
        if r.returncode != 0:
            return []
        out: list[dict[str, Any]] = []
        # -z output: status\0path\0status\0path\0...  (rename adds an extra path)
        tokens = [t for t in r.stdout.split("\x00") if t != ""]
        i = 0
        while i < len(tokens):
            status = tokens[i][0]
            if status == "R" and i + 2 < len(tokens):
                path = tokens[i + 2]  # new path
                i += 3
            else:
                path = tokens[i + 1] if i + 1 < len(tokens) else ""
                i += 2
            if not path:
                continue
            old = self._show(parent, path) if status != "A" else None
            new = self._show(checkpoint_id, path) if status != "D" else None
            out.append({
                "path": path,
                "status": "A" if status == "R" else status,
                "old_content": old or "",
                "new_content": new or "",
            })
        return out

    # -- restore -----------------------------------------------------------

    def restore(self, checkpoint_id: str, session_id: str) -> dict[str, Any]:
        """Restore the work-tree to a checkpoint — only files THIS session touched.

        The shadow repo is shared across every session in a working directory, so
        the naive "diff checkpoint..HEAD" would treat files touched by *other*
        interleaved sessions as this session's own and revert them — silently
        destroying the other sessions' work. Instead we compute the file set from
        only this session's own commits after the checkpoint (see
        _session_touched_since) and act solely on those:

        - path exists at the checkpoint → check it out (rewind to that version)
        - path absent at the checkpoint → this session created it later → delete

        Files another session added/modified after the checkpoint are never in
        the set, so they're left untouched. When another session *also* changed a
        file this session touched (a genuine cross-session conflict), that path is
        reported in ``conflicts`` — the rewind still applies this session's view,
        but the caller can warn the user.
        """
        if not self.ensure_init():
            return {"restored": [], "deleted": [], "available": False}

        touched = self._session_touched_since(checkpoint_id, session_id)
        others = self._other_sessions_touched_since(checkpoint_id, session_id)
        # Two kinds of conflict a rewind would clobber:
        # 1. Files another session already committed after the checkpoint.
        # 2. Files with uncommitted on-disk drift vs HEAD — an edit no snapshot
        #    has captured yet (another session or the user changed it mid-turn,
        #    before its own checkpoint fired). MUST be computed BEFORE the
        #    "before restore" snapshot below, which commits that drift away.
        conflicts = sorted((touched & others) | self._uncommitted_paths(touched))

        # Snapshot the current state of ONLY our files first, so this rewind is
        # itself reversible without absorbing other sessions' on-disk changes
        # (a plain `add -A` here would pull their files into our history — the
        # exact bug this method exists to avoid). See _snapshot_paths. This also
        # preserves any uncommitted drift detected above, so a clobbered change
        # remains recoverable from history.
        self._snapshot_paths(session_id, "before restore", touched)

        restored: list[str] = []
        deleted: list[str] = []
        for path in sorted(touched):
            # Does the target checkpoint have a version of this file?
            exists_at_ckpt = self._run(
                "cat-file", "-e", f"{checkpoint_id}:{path}"
            ).returncode == 0
            if exists_at_ckpt:
                co = self._run("checkout", checkpoint_id, "--", path)
                if co.returncode == 0:
                    restored.append(path)
            else:
                target = Path(self._cwd) / path
                try:
                    if target.exists():
                        target.unlink()
                        deleted.append(path)
                except OSError:
                    pass

        # Record the post-restore state (again scoped to our files) so the
        # rewind itself is reversible.
        self._snapshot_paths(session_id, "after restore", touched)
        result: dict[str, Any] = {"restored": restored, "deleted": deleted}
        if conflicts:
            result["conflicts"] = conflicts
        return result


def delete_shadow_repo_for_cwd(
    cwd: str, checkpoints_root: str | Path | None = None
) -> bool:
    """Delete the shadow repo for a working directory, if one exists.

    The shadow repo is keyed by ``sha1(resolve(cwd))`` — the exact derivation in
    ``CheckpointManager.__init__`` — and is shared across every session in that
    directory. It is therefore only safe to remove once no session in that
    directory remains: a still-live session's stored checkpoint SHAs would
    otherwise dangle. Callers (session deletion) must establish that precondition
    first; this helper performs no session bookkeeping of its own.

    ``checkpoints_root`` overrides the default ``~/.cluxmate/checkpoints`` (used
    by tests to isolate the real shadow-repo root). Returns True when a repo was
    actually removed, False when there was nothing to remove.
    """
    root = (
        Path(checkpoints_root)
        if checkpoints_root is not None
        else Path.home() / ".cluxmate" / "checkpoints"
    )
    digest = hashlib.sha1(str(Path(cwd).resolve()).encode("utf-8")).hexdigest()
    repo = root / f"{digest}.git"
    if repo.is_dir():
        CheckpointManager._rmtree(repo)
        return True
    return False
