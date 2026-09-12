"""Regression proofs for the unload contract (bug 4.2, 2026-08-18).

Three properties, tested against the FakeEngine:
  1. A draining scheduler admits NOTHING new (ModelUnloadingError).
  2. abort_active() really unwinds in-flight consumers and empties the
     ledger — the refcount reaches zero, so the unload may proceed.
  3. wait_idle() reports the truth: False while work runs, True after.
"""

import asyncio
import contextlib

import pytest

from seymour.engine.adapter import GenerationRequest
from seymour.engine.fake import FakeEngine
from seymour.scheduler.core import Scheduler
from seymour.scheduler.tiers import ModelUnloadingError, Tier


def req() -> GenerationRequest:
    """A minimal request; the fake engine ignores its content."""
    return GenerationRequest(messages=[{"role": "user", "content": "hi"}],
                             max_tokens=8)


async def make_scheduler(slots: int = 2, delay: float = 0.005, tokens: int = 200):
    """A concurrent-mode scheduler on a slow-ish fake engine."""
    engine = FakeEngine(concurrent=True, total_slots=slots,
                        delay=delay, tokens=tokens)
    caps = await engine.capabilities()
    return Scheduler(engine, caps), engine


@pytest.mark.asyncio
async def test_drain_blocks_new_admissions():
    """begin_drain() → any new stream is refused with ModelUnloadingError,
    and end_drain() reopens the door."""
    scheduler, _ = await make_scheduler()
    scheduler.begin_drain()
    with pytest.raises(ModelUnloadingError):
        async for _ in scheduler.stream(Tier.LIVE_CHAT, req(), "chat:new"):
            pass
    scheduler.end_drain()
    tokens = [t async for t in scheduler.stream(Tier.LIVE_CHAT, req(), "chat:ok")]
    assert tokens                    # admitted and streamed after end_drain


@pytest.mark.asyncio
async def test_abort_active_reaches_refcount_zero():
    """In-flight consumers are cancelled for real: the ledger empties and
    wait_idle() confirms it — the unload's precondition."""
    scheduler, _ = await make_scheduler(slots=2, delay=0.01, tokens=1000)

    async def consume(tier: Tier, label: str) -> None:
        async for _ in scheduler.stream(tier, req(), label):
            pass

    # Two different tiers, so per-tier caps can't halve the test.
    tasks = [asyncio.create_task(consume(Tier.LIVE_CHAT, "chat:live")),
             asyncio.create_task(consume(Tier.FOREGROUND_TASK, "job:one"))]
    # Let both streams genuinely start (hold tickets, stream tokens).
    for _ in range(50):
        await asyncio.sleep(0.01)
        if len(scheduler.snapshot()["active"]) == 2:
            break
    assert len(scheduler.snapshot()["active"]) == 2
    assert not await scheduler.wait_idle(timeout=0.05)   # truthfully busy

    scheduler.begin_drain()
    assert scheduler.abort_active() == 2                 # both told to stop
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert await scheduler.wait_idle(timeout=2.0)        # refcount zero
    assert scheduler.snapshot()["active"] == []
    assert scheduler.snapshot()["queued"] == []


@pytest.mark.asyncio
async def test_abort_active_cancels_queued_waiters():
    """A consumer still WAITING for a slot is aborted too — queued work
    must not survive a cancel-and-unload and grab the engine later."""
    scheduler, _ = await make_scheduler(slots=1, delay=0.01, tokens=1000)

    async def consume(label: str) -> None:
        async for _ in scheduler.stream(Tier.FOREGROUND_TASK, req(), label):
            pass

    holder = asyncio.create_task(consume("job:holding"))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if scheduler.snapshot()["active"]:
            break
    queued = asyncio.create_task(consume("job:queued"))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if scheduler.snapshot()["queued"]:
            break
    assert scheduler.snapshot()["queued"]

    scheduler.begin_drain()
    scheduler.abort_active()
    for task in (holder, queued):
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert await scheduler.wait_idle(timeout=2.0)
