"""Write fence — canonicalize-then-contain path guard for file-write tools.

This is Phase 0 of the sandbox plan (docs/plans/sandbox-threat-model.md): a
T1 boundary that constrains the *values* the model supplies (path strings).
It is NOT a defense against malicious code — a determined process can bypass
any in-process check. It prevents the model from accidentally or (via prompt
injection) deliberately writing/deleting outside the workspace.

Design (mirrors DSH's `writableRoots`):
- The writable roots are defined in exactly ONE place: `WriteFence.roots()`.
  Default roots = the session working directory + the platform temp dir +
  one home file: ~/.cluxmate/AGENTS.md (the global memory file — see the
  denyroots note below).
- `<cwd>/.cluxmate/` is a DENY subtree inside the workspace: it holds
  CluxMate's own privileged project state (permissions.json — the always-
  allow list, mcp.json — spawns subprocesses on load, skills.json). A model
  steered by prompt injection must not be able to edit its own permission
  config, so the deny list takes precedence over the writable roots.
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

from ._sandbox import ESCALATION_HINT


class SandboxViolation(Exception):
    """A tool attempted to modify a path outside the writable roots."""


def _global_memory_file() -> Path:
    """The one home file writable through the fence: global AGENTS.md."""
    return (Path.home() / ".cluxmate" / "AGENTS.md").resolve()


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
                 grant_paths: list[str] | None = None):
        self._workdir = workdir
        self.enabled = enabled
        self._grant_paths = grant_paths or []

    def roots(self) -> list[Path]:
        """Writable roots: the working directory (implicit) + the platform
        temp dir + the global memory file (exact file) + any user-granted
        folders (the sandbox-grants.json registry).

        Resolved so comparison against a resolved target is apples-to-apples.
        """
        base = Path(self._workdir) if self._workdir else Path.cwd()
        roots = [
            base.resolve(),
            Path(tempfile.gettempdir()).resolve(),
            _global_memory_file(),
        ]
        for g in self._grant_paths:
            try:
                roots.append(Path(g).resolve())
            except OSError:
                continue
        return roots

    def denyroots(self) -> list[Path]:
        """Subtrees that are NEVER writable, even though they sit inside a
        writable root: CluxMate's own per-project state directory
        ``<cwd>/.cluxmate/`` (permissions / MCP config / skill toggles —
        a model must not edit its own permission config)."""
        base = Path(self._workdir) if self._workdir else Path.cwd()
        return [(base / ".cluxmate").resolve()]

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
        if any(
            resolved == d or resolved.is_relative_to(d)
            for d in self.denyroots()
        ):
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
