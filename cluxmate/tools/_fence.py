"""Write fence — canonicalize-then-contain path guard for file-write tools.

This is Phase 0 of the sandbox plan (docs/plans/sandbox-threat-model.md): a
T1 boundary that constrains the *values* the model supplies (path strings).
It is NOT a defense against malicious code — a determined process can bypass
any in-process check. It prevents the model from accidentally or (via prompt
injection) deliberately writing/deleting outside the workspace.

Design:
- The writable roots are defined in exactly ONE place: `WriteFence.roots()`.
  Default roots = the session working directory + the platform temp dir +
  one home file: ~/.cluxmate/AGENTS.md (the global memory file — see the
  denyroots note below) + the project memory file <root>/AGENTS.md (when the
  session runs in a git worktree, the project root is the MAIN worktree).
- `<cwd>/.cluxmate/` is a DENY subtree inside the workspace: it holds
  CluxMate's own privileged project state (permissions.json — the always-
  allow list, mcp.json — spawns subprocesses on load, skills.json). A model
  steered by prompt injection must not be able to edit its own permission
  config, so the deny list takes precedence over the writable roots. The
  PROJECT's state dir and the `<root>/.worktrees/` container are deny subtrees
  too; see `WriteFence.denyroots()` and `_deny_violation()`.
- ~/.cluxmate/AGENTS.md (exactly this file, NOT the whole directory) is
  whitelisted because update_memory's documented contract says "to correct
  or delete a global entry, edit it with search_replace" — without the
  whitelist that instruction is unenforceable. The rest of ~/.cluxmate
  (config.json with API keys, session logs, checkpoints, the desktop DB)
  stays off-limits.
- Every write/delete tool canonicalizes its target (`Path.resolve(strict=False)`
  resolves symlinks and ../ segments) BEFORE the containment check, so a
  symlink or `..`-chain pointing outside the roots is rejected.
- The fence is enforced in all modes except "yolo" (the documented
  danger-equivalent, where the user has explicitly opted out of guardrails).
  Mode is baked in per agent build; `chat/set_mode` rebuilds the agent, so a
  mode switch re-arms/disarms the fence with the new toolset.

Comparison note: `PureWindowsPath` comparison is case-insensitive, so
`is_relative_to` gives correct Windows semantics without extra normcase work.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from ..core.read_denies import is_sensitive_pattern
from ._sandbox import ESCALATION_HINT


class SandboxViolation(Exception):
    """A tool attempted to modify a path outside the writable roots."""


def _global_memory_file() -> Path:
    """The one home file writable through the fence: global AGENTS.md."""
    return (Path.home() / ".cluxmate" / "AGENTS.md").resolve()


# Project memory file name; aligned with core/memory.py:MEMORY_FILENAME.
PROJECT_MEMORY_FILENAME = "AGENTS.md"


class WriteFence:
    """Containment fence for file-write/delete tools.

    ``check(path)`` returns the canonicalized path when it is inside one of
    the writable roots, and raises :class:`SandboxViolation` otherwise.
    With ``enabled=False`` (yolo mode) every path passes through unchanged —
    the fence is fully disabled, matching the mode's contract.

    Precedence: deny subtrees win over writable roots; the single-file
    whitelist is exact-match only (its parent directory is NOT writable).
    """

    def __init__(self, workdir: str | None, enabled: bool = True,
                 grant_paths: list[str] | None = None,
                 project_root: str | None = None):
        self._workdir = workdir
        self.enabled = enabled
        self._grant_paths = grant_paths or []
        # The project root (main worktree) when the session runs in a git
        # worktree. Only the CONFIG-state rules use it; the writable tree is
        # still the session cwd. None = not a worktree (root == cwd).
        self._project_root = project_root

    def _base(self) -> Path:
        return Path(self._workdir) if self._workdir else Path.cwd()

    def _project_base(self) -> Path:
        if self._project_root:
            return Path(self._project_root).resolve()
        return self._base().resolve()

    def roots(self) -> list[Path]:
        """Writable roots: the session tree (implicit) + the platform temp dir
        + the global memory file + the PROJECT memory file (exact file — a
        worktree session edits the main repo's AGENTS.md) + user-granted folders.
        """
        base = self._base().resolve()
        roots = [
            base,
            Path(tempfile.gettempdir()).resolve(),
            _global_memory_file(),
        ]
        project_memory = self._project_base() / PROJECT_MEMORY_FILENAME
        if project_memory not in roots:
            roots.append(project_memory)
        for g in self._grant_paths:
            try:
                roots.append(Path(g).resolve())
            except OSError:
                continue
        return roots

    def _worktrees_root(self) -> Path:
        return (self._project_base() / ".worktrees").resolve()

    def denyroots(self) -> list[Path]:
        """Subtrees that are NEVER writable, even inside a writable root:
        the session's own state dir, the PROJECT's state dir (a model must not
        edit its own permission config, in either tree) and the worktree
        container (see _deny_violation for its single carve-out)."""
        base = self._base().resolve()
        out: list[Path] = []
        for d in (
            (base / ".cluxmate").resolve(),
            (self._project_base() / ".cluxmate").resolve(),
            self._worktrees_root(),
        ):
            if d not in out:
                out.append(d)
        return out

    def _deny_violation(self, resolved: Path) -> Path | None:
        """Which deny root rejects this path, or None.

        The worktree container carries exactly ONE carve-out: the session's own
        tree inside it stays writable (otherwise a worktree session could write
        nothing at all). Sibling worktrees stay denied — and because this runs
        BEFORE the escalation short-circuit, they stay denied under
        danger-full-access too. Note the carve-out needs BOTH conditions: the
        session cwd must be inside the container, otherwise a main-tree session
        (cwd == <root>) would swallow the whole deny root.
        """
        base = self._base().resolve()
        worktrees = self._worktrees_root()
        for d in self.denyroots():
            if not (resolved == d or resolved.is_relative_to(d)):
                continue
            if d == worktrees and base.is_relative_to(d) and resolved.is_relative_to(base):
                continue
            return d
        return None

    def check(self, path: Path, escalate: bool = False) -> Path:
        """Canonicalize ``path`` and enforce containment.

        Returns the resolved path (callers should use it for the actual write
        so the checked path and the written path cannot diverge). Raises
        SandboxViolation when the target escapes every writable root —
        including via symlink, since resolve() follows links — or when it
        lands in a deny subtree.

        ``escalate=True`` grants the danger-full-access semantics: containment
        against the writable roots is SKIPPED (the user already approved the
        wider mode), but the deny subtrees STILL apply — editing the agent's
        own permission/config directory is a privilege-escalation vector, not
        a one-off write, so it is never reachable through a file tool.
        """
        if not self.enabled:
            return path
        resolved = path.resolve(strict=False)
        denied = self._deny_violation(resolved)
        if denied is not None:
            raise SandboxViolation(
                f"path is inside a protected directory and not writable "
                f"through the sandbox: {path} (resolved: {resolved}; "
                f"protected: {', '.join(str(d) for d in self.denyroots())})"
            )
        if escalate:
            return resolved
        for root in self.roots():
            if resolved == root or resolved.is_relative_to(root):
                return resolved
        raise SandboxViolation(
            f"path is outside the writable sandbox: {path} "
            f"(resolved: {resolved}; writable roots: "
            f"{', '.join(str(r) for r in self.roots())})\n"
            f"{ESCALATION_HINT}"
        )

    def check_message(self, path: Path, escalate: bool = False) -> str:
        """Non-raising variant returning the error message, or '' if allowed.

        Convenient for the multi-file tools, which report per-item errors in
        their result lines rather than failing the whole call.
        """
        try:
            self.check(path, escalate=escalate)
        except SandboxViolation as e:
            return str(e)
        return ""


class ReadDenied(Exception):
    """A read tool attempted to read a path inside a forbid_read subtree.

    The message deliberately does NOT echo the denylist contents — the model
    already knows which path it requested, and printing the configured hidden
    folders would leak the very secrets the list is meant to protect.
    """


class ReadFence:
    """Read-side denylist fence for the read tools (read_file / grep / list_dir).

    This is the read sibling of :class:`WriteFence`, but it is a DENYLIST
    (default empty → no-op), not an allowlist: a coding agent must be able to
    read arbitrary files by default. Two sources can block a read:

    - ``deny_paths`` — the user's ``~/.cluxmate/forbid-read.json`` paths (plus
      the built-in sensitive DIRECTORIES when ``protect_sensitive`` is on, via
      the store's ``effective_paths()``).
    - ``protect_sensitive=True`` — the built-in sensitive-file PATTERN rules
      (``.env`` / ``.git-credentials`` / ``.netrc`` / ``*.pem`` / ``*.key`` /
      ``*.p12`` / ``*.pfx``), matched by basename anywhere on disk. Off by
      default (zero behavior change); the pattern rules are process-level only
      (no shell-side equivalent).

    Canonicalize-then-contain, same as WriteFence: ``resolve(strict=False)``
    first so ``..`` and symlinks cannot dodge the deny list (a workspace link
    pointing at ``~/.ssh`` resolves to the deny root and is rejected).

    The deny roots are files OR directories: a file root blocks that exact
    file; a directory root blocks the whole subtree.
    """

    def __init__(self, deny_paths: list[str] | None = None,
                 protect_sensitive: bool = False):
        self._deny_paths: list[Path] = []
        for p in deny_paths or []:
            try:
                self._deny_paths.append(Path(p).resolve())
            except OSError:
                continue
        self._protect_sensitive = protect_sensitive

    @property
    def enabled(self) -> bool:
        """True when at least one deny root or the pattern rules are active."""
        return bool(self._deny_paths) or self._protect_sensitive

    def denyroots(self) -> list[Path]:
        return list(self._deny_paths)

    def is_denied(self, path: Path) -> bool:
        """Non-raising: is ``path`` (or its resolved target) inside a deny root,
        or matching a built-in sensitive-file pattern?

        Used by grep's directory walk and list_dir's entry filtering, where a
        single forbidden subtree should be skipped silently rather than fail
        the whole operation.
        """
        if not self._deny_paths and not self._protect_sensitive:
            return False
        try:
            resolved = path.resolve(strict=False)
        except OSError:
            resolved = Path(path)
        if self._protect_sensitive and is_sensitive_pattern(resolved):
            return True
        return any(
            resolved == d or resolved.is_relative_to(d) for d in self._deny_paths
        )

    def check(self, path: Path) -> Path:
        """Canonicalize ``path`` and raise :class:`ReadDenied` if it is denied.

        Returns the resolved path (callers should read the resolved path so the
        checked path and the read path cannot diverge via a symlink swap).
        """
        resolved = path.resolve(strict=False)
        if self._protect_sensitive and is_sensitive_pattern(resolved):
            raise ReadDenied(
                f"reading this path is forbidden by the sensitive-file "
                f"protection rules: {path}"
            )
        if any(
            resolved == d or resolved.is_relative_to(d) for d in self._deny_paths
        ):
            raise ReadDenied(
                f"reading this path is forbidden by the read-denylist: {path}"
            )
        return resolved
