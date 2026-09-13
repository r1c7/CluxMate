"""Admission control for subagents: concurrency caps + write-path claims.

Two problems, one gate:

- Unbounded fan-out: a step with N `task` calls starts N full agents at once,
  each with its own streaming request;
- Write conflicts: nothing stopped two subagents (or a subagent and the parent)
  from writing the same file concurrently.

Design (mirrors Reasonix `internal/agent/scheduler.go` + `write_claims.go`):

- a hard cap on running subagents and on concurrently *writing* subagents;
- a spawning call declares its write scope (`task`'s optional `write_paths`);
  omitting it claims the WHOLE workspace, so parallel writers serialize by
  default and only an explicit disjoint declaration buys parallelism;
- read-only subagents claim nothing and never block each other;
- a NESTED spawn (issued by a subagent) that finds no slot fails fast instead of
  queueing — a queued child would hold its parent's slot while waiting for one,
  which is a deadlock.

The scheduler is per event loop: the JSON-RPC server runs every turn on a fresh
loop, and an asyncio primitive created on a dead loop cannot be awaited on a new
one. See AgentBuilder._scheduler_for_loop.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from pathlib import Path

# Concurrency caps. Constants for now (no config surface until there is a
# settings page that would own it).
MAX_CONCURRENT = 4
MAX_WRITERS = 2


def claims_overlap(a: frozenset[Path], b: frozenset[Path]) -> bool:
    """True when any path in ``a`` equals, contains, or is contained by one in ``b``."""
    for x in a:
        for y in b:
            if x == y or x in y.parents or y in x.parents:
                return True
    return False


def workspace_claim(cwd: str) -> frozenset[Path]:
    """The claim used when a writing subagent declares nothing: everything."""
    return frozenset({Path(cwd).resolve()})


def normalize_claim(cwd: str, paths: list[str]) -> tuple[frozenset[Path], list[str]]:
    """Resolve declared ``write_paths`` against the workspace.

    Returns ``(claim, dropped)``. Entries resolving outside the workspace are
    dropped: the sandbox may also write to temp and user-granted folders, and
    comparing those against a workspace claim would be meaningless.
    """
    root = Path(cwd).resolve()
    claim: set[Path] = set()
    dropped: list[str] = []
    for raw in paths or []:
        if not isinstance(raw, str) or not raw.strip():
            continue
        resolved = (root / raw).resolve()
        if resolved == root or root in resolved.parents:
            claim.add(resolved)
        else:
            dropped.append(raw)
    return frozenset(claim), dropped


@dataclass
class Ticket:
    """A granted slot. Release it exactly once, in a ``finally``."""

    slug: str
    claim: frozenset[Path]
    released: bool = False


@dataclass
class _Waiter:
    slug: str
    claim: frozenset[Path]
    fut: asyncio.Future


class SubagentScheduler:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self._running = 0
        self._writers = 0
        self._running_claims: list[tuple[str, frozenset[Path]]] = []
        self._waiters: deque[_Waiter] = deque()

    # --- introspection (tests / diagnostics) ---
    @property
    def running(self) -> int:
        return self._running

    @property
    def writers(self) -> int:
        return self._writers

    @property
    def queued(self) -> int:
        return len(self._waiters)

    def _admissible(self, claim: frozenset[Path]) -> bool:
        if self._running >= MAX_CONCURRENT:
            return False
        if not claim:
            return True
        if self._writers >= MAX_WRITERS:
            return False
        return not any(
            claims_overlap(claim, held) for _slug, held in self._running_claims
        )

    def _grant(self, slug: str, claim: frozenset[Path]) -> None:
        self._running += 1
        if claim:
            self._writers += 1
            self._running_claims.append((slug, claim))

    async def acquire(
        self, claim: frozenset[Path], *, nested: bool, slug: str
    ) -> Ticket | None:
        """Take a slot (and a write claim when non-empty).

        Returns None only for a NESTED spawn that found no slot — the caller
        turns that into an error result for the model.
        """
        if self._admissible(claim):
            self._grant(slug, claim)
            return Ticket(slug=slug, claim=claim)
        if nested:
            return None
        waiter = _Waiter(slug=slug, claim=claim, fut=self.loop.create_future())
        self._waiters.append(waiter)
        try:
            await waiter.fut
        except asyncio.CancelledError:
            if waiter.fut.done() and not waiter.fut.cancelled():
                # Granted in the same tick the caller was cancelled: hand it back.
                self.release(Ticket(slug=slug, claim=claim))
            elif waiter in self._waiters:
                self._waiters.remove(waiter)
            raise
        return Ticket(slug=slug, claim=claim)

    def release(self, ticket: Ticket) -> None:
        """Free a slot (idempotent) and admit whatever can now run."""
        if ticket.released:
            return
        ticket.released = True
        self._running -= 1
        if ticket.claim:
            self._writers -= 1
            try:
                self._running_claims.remove((ticket.slug, ticket.claim))
            except ValueError:
                pass
        self._pump()

    def _pump(self) -> None:
        """Admit queued spawns in arrival order, skipping ones still blocked.

        Skipping (instead of stopping at the first blocked waiter) lets a later
        read-only spawn through while a big writer waits for its region to free
        up — head-of-line blocking on a claim nobody can satisfy yet is worse
        than mild reordering.
        """
        i = 0
        while i < len(self._waiters):
            w = self._waiters[i]
            if not self._admissible(w.claim):
                i += 1
                continue
            self._waiters.remove(w)
            self._grant(w.slug, w.claim)
            w.fut.set_result(True)
