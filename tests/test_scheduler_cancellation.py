"""Regression proofs for the two scheduler bugs the adversarial review
confirmed (2026-08): the cancelled-waiter ledger leak, and preemption on
behalf of a waiter whose tier was already at its cap.

Both are timing bugs, so both are tested the only honest way: against the
FakeEngine, with the timings under the test's control.
"""

import asyncio
import contextlib

import pytest

from seymour.engine.adapter import GenerationRequest
from seymour.engine.fake import FakeEngine
from seymour.scheduler.core import Scheduler
from seymour.scheduler.tiers import PreemptedError, Tier


def req() -> GenerationRequest:
    """A minimal request; the fake engine ignores its content."""
    return GenerationRequest(messages=[{"role": "user", "content": "hi"}],
                             max_tokens=8)


async def make_scheduler(slots: int = 2, delay: float = 0.005, tokens: int = 30):
    """A concurrent-mode scheduler on a fake engine (same as test_scheduler)."""
    engine = FakeEngine(concurrent=True, total_slots=slots,
                        delay=delay, tokens=tokens)
    caps = await engine.capabilities()
    return Scheduler(engine, caps), engine


async def consume(scheduler, tier, label=""):
    """Run one stream to completion; return its collected tokens."""
    return [t async for t in scheduler.stream(tier, req(), label)]


# --------------------------------------------------------------------------- #
#  Bug 1 — a consumer cancelled while QUEUED must leave no trace              #
# --------------------------------------------------------------------------- #

async def test_cancelled_waiter_leaves_no_trace():
    scheduler, _ = await make_scheduler(slots=2, tokens=30)
    # Fill both slots with live chat (chat may hold every slot).
    c1 = asyncio.create_task(consume(scheduler, Tier.LIVE_CHAT, "c1"))
    c2 = asyncio.create_task(consume(scheduler, Tier.LIVE_CHAT, "c2"))
    await asyncio.sleep(0.02)              # both seated
    # A third chat request queues behind them…
    c3 = asyncio.create_task(consume(scheduler, Tier.LIVE_CHAT, "c3"))
    await asyncio.sleep(0.01)              # …and is now in the waiters list
    assert scheduler.snapshot()["waiting"]["live_chat"] == 1
    # …then its consumer dies (the browser tab closed while queued).
    c3.cancel()
    with pytest.raises(asyncio.CancelledError):
        await c3
    # The dead waiter must be GONE from the ledger immediately.
    assert scheduler.snapshot()["waiting"]["live_chat"] == 0
    # When the seated streams finish, every slot must come back — before
    # the fix, a freed slot could be granted to the dead waiter and its
    # ticket would never be released (a permanently lost slot).
    await asyncio.gather(c1, c2)
    assert len(scheduler._tickets) == 0
    # And the machine still works at full capacity afterwards.
    assert len(await consume(scheduler, Tier.LIVE_CHAT, "again")) == 30


async def test_cancelled_agent_waiter_stops_reserving_floor_slots():
    scheduler, _ = await make_scheduler(slots=2, tokens=30)
    # The agent takes a slot; a queued agent request then dies while waiting.
    a1 = asyncio.create_task(consume(scheduler, Tier.BACKGROUND_AGENT, "a1"))
    c1 = asyncio.create_task(consume(scheduler, Tier.LIVE_CHAT, "c1"))
    await asyncio.sleep(0.02)              # both slots held
    a2 = asyncio.create_task(consume(scheduler, Tier.BACKGROUND_AGENT, "a2"))
    await asyncio.sleep(0.01)              # a2 queued (no free slot)
    a2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await a2
    await asyncio.gather(a1, c1)
    # Before the fix the phantom agent waiter kept `agent_demand` true
    # forever, permanently reserving a floor slot away from chat. Two
    # simultaneous chats must be able to hold BOTH slots again.
    d1 = asyncio.create_task(consume(scheduler, Tier.LIVE_CHAT, "d1"))
    d2 = asyncio.create_task(consume(scheduler, Tier.LIVE_CHAT, "d2"))
    await asyncio.sleep(0.02)
    assert scheduler.held(Tier.LIVE_CHAT) == 2
    await asyncio.gather(d1, d2)


async def test_repeated_cancel_races_never_leak_slots():
    """Abuse the cancellation window at varying offsets: whatever the
    interleaving with a grant, the ledger must end clean every round."""
    scheduler, _ = await make_scheduler(slots=2, tokens=10)
    for round_number in range(6):
        c1 = asyncio.create_task(consume(scheduler, Tier.LIVE_CHAT, "c1"))
        c2 = asyncio.create_task(consume(scheduler, Tier.LIVE_CHAT, "c2"))
        await asyncio.sleep(0.01)          # both seated
        c3 = asyncio.create_task(consume(scheduler, Tier.LIVE_CHAT, "c3"))
        # Vary the cancel timing across rounds to explore the race window
        # (including right around when c1/c2 finish and a grant fires).
        await asyncio.sleep(0.008 * round_number)
        c3.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await c3
        await asyncio.gather(c1, c2)
        # After every round: nothing waiting, nothing held.
        assert sum(scheduler.snapshot()["waiting"].values()) == 0
        assert len(scheduler._tickets) == 0
    # Full capacity survives six rounds of abuse.
    assert len(await consume(scheduler, Tier.LIVE_CHAT, "final")) == 10


# --------------------------------------------------------------------------- #
#  Bug 2 — no victim is evicted for a waiter already at its tier cap          #
# --------------------------------------------------------------------------- #

async def test_no_eviction_for_waiter_at_tier_cap():
    # Four slots: research holds its full cap (2), the agent holds the rest.
    scheduler, _ = await make_scheduler(slots=4, tokens=60)
    cap = scheduler.policy.tier_caps[Tier.FOREGROUND_TASK]
    assert cap == 2                        # the shipped default this test needs
    agent_preempted: list[str] = []

    async def agent_stream(name: str):
        try:
            await consume(scheduler, Tier.BACKGROUND_AGENT, name)
        except PreemptedError:
            agent_preempted.append(name)

    agents = [asyncio.create_task(agent_stream(f"a{i}")) for i in range(2)]
    research = [asyncio.create_task(consume(scheduler, Tier.FOREGROUND_TASK, f"r{i}"))
                for i in range(cap)]
    await asyncio.sleep(0.03)              # all four slots held
    # A THIRD research request arrives. Its tier is at cap, so it must
    # WAIT — before the fix it evicted an agent stream, was refused the
    # freed slot (cap check), and the agent's work died for nothing.
    extra = asyncio.create_task(consume(scheduler, Tier.FOREGROUND_TASK, "r-extra"))
    await asyncio.sleep(0.05)              # plenty of pump cycles
    assert agent_preempted == [], "agent was evicted for a capped waiter"
    # Once a research slot frees legitimately, the extra request runs.
    await asyncio.gather(*research, extra, *agents)
    assert len(scheduler._tickets) == 0
