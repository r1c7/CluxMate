"""Subagent admission control: caps, write claims, queueing, nested fail-fast."""

import asyncio
from pathlib import Path

import pytest

from cluxmate.core.subagent_scheduler import (
    SubagentScheduler,
    claims_overlap,
    normalize_claim,
    workspace_claim,
)


def test_claims_overlap_rules(tmp_path):
    src = (tmp_path / "src").resolve()
    app = (tmp_path / "src" / "app.py").resolve()
    tests_dir = (tmp_path / "tests").resolve()
    assert claims_overlap(frozenset({src}), frozenset({app}))
    assert claims_overlap(frozenset({app}), frozenset({src}))
    assert claims_overlap(frozenset({app}), frozenset({app}))
    assert not claims_overlap(frozenset({app}), frozenset({tests_dir}))


def test_normalize_claim_keeps_inside_drops_escapes(tmp_path):
    claim, dropped = normalize_claim(str(tmp_path), ["src", "app.py", "../outside", ""])
    assert (tmp_path / "src").resolve() in claim
    assert (tmp_path / "app.py").resolve() in claim
    assert dropped == ["../outside"]


def test_workspace_claim_is_the_root(tmp_path):
    assert workspace_claim(str(tmp_path)) == frozenset({tmp_path.resolve()})


@pytest.mark.asyncio
async def test_cap_queues_then_admits(tmp_path, monkeypatch):
    monkeypatch.setattr("cluxmate.core.subagent_scheduler.MAX_CONCURRENT", 1)
    s = SubagentScheduler(asyncio.get_running_loop())
    t1 = await s.acquire(frozenset(), nested=False, slug="explore")
    assert t1 is not None and s.running == 1

    task = asyncio.create_task(s.acquire(frozenset(), nested=False, slug="explore"))
    await asyncio.sleep(0)
    assert s.queued == 1 and not task.done()

    s.release(t1)
    t2 = await asyncio.wait_for(task, 1)
    assert t2 is not None and s.running == 1
    assert s.queued == 0


@pytest.mark.asyncio
async def test_nested_spawn_fails_fast_when_full(monkeypatch):
    monkeypatch.setattr("cluxmate.core.subagent_scheduler.MAX_CONCURRENT", 1)
    s = SubagentScheduler(asyncio.get_running_loop())
    t1 = await s.acquire(frozenset(), nested=False, slug="explore")
    assert await s.acquire(frozenset(), nested=True, slug="explore") is None
    assert s.queued == 0          # nothing left behind
    s.release(t1)


@pytest.mark.asyncio
async def test_writer_claim_blocks_overlapping_but_not_readers(tmp_path):
    s = SubagentScheduler(asyncio.get_running_loop())
    t1 = await s.acquire(workspace_claim(str(tmp_path)), nested=False, slug="general-purpose")
    assert s.writers == 1

    blocked = asyncio.create_task(s.acquire(
        frozenset({(tmp_path / "src").resolve()}), nested=False, slug="general-purpose",
    ))
    await asyncio.sleep(0)
    assert not blocked.done()

    reader = await s.acquire(frozenset(), nested=False, slug="explore")
    assert reader is not None

    s.release(t1)
    assert await asyncio.wait_for(blocked, 1) is not None
    s.release(reader)


@pytest.mark.asyncio
async def test_writer_cap_serializes_disjoint_claims(tmp_path, monkeypatch):
    monkeypatch.setattr("cluxmate.core.subagent_scheduler.MAX_WRITERS", 1)
    s = SubagentScheduler(asyncio.get_running_loop())
    t1 = await s.acquire(frozenset({(tmp_path / "a").resolve()}), nested=False, slug="x")
    task = asyncio.create_task(s.acquire(
        frozenset({(tmp_path / "b").resolve()}), nested=False, slug="x",
    ))
    await asyncio.sleep(0)
    assert not task.done()        # disjoint, but the writer cap is full
    s.release(t1)
    assert await asyncio.wait_for(task, 1) is not None


@pytest.mark.asyncio
async def test_cancelling_a_queued_spawn_leaves_no_waiter(monkeypatch):
    monkeypatch.setattr("cluxmate.core.subagent_scheduler.MAX_CONCURRENT", 1)
    s = SubagentScheduler(asyncio.get_running_loop())
    t1 = await s.acquire(frozenset(), nested=False, slug="explore")
    task = asyncio.create_task(s.acquire(frozenset(), nested=False, slug="explore"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert s.queued == 0
    s.release(t1)
    assert s.running == 0


@pytest.mark.asyncio
async def test_double_release_is_a_noop():
    s = SubagentScheduler(asyncio.get_running_loop())
    t = await s.acquire(frozenset(), nested=False, slug="explore")
    s.release(t)
    s.release(t)
    assert s.running == 0
