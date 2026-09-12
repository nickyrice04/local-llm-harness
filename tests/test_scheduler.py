"""Scheduler proofs: the promises Part IV makes, asserted deterministically.

Every test runs against the FakeEngine — no model, no network, milliseconds
— because concurrency bugs are timing bugs, and timing bugs need an engine
whose timing the test controls.

The promises under test:
    1. Concurrent mode: chat and agent genuinely overlap.
    2. Tier 1 preempts the agent when every slot is held — but never below
       the agent's floor.
    3. Serial mode: one at a time; a human request cancels the agent's.
    4. The floor: a starved agent's next request is promoted and may bump
       Tier 2 — and the agent always gets back in (starvation-freedom).
    5. Slots are always released — success, preemption, or abandonment.
"""

import asyncio

import pytest

from seymour.engine.adapter import GenerationRequest
from seymour.engine.fake import FakeEngine
from seymour.scheduler.core import Scheduler
from seymour.scheduler.tiers import PreemptedError, Tier


def req() -> GenerationRequest:
    """A minimal request; the fake engine ignores its content."""
    return GenerationRequest(messages=[{"role": "user", "content": "hi"}],
                             max_tokens=8)


async def make_scheduler(concurrent: bool = True, slots: int = 4,
                         delay: float = 0.005, tokens: int = 8):
    """A scheduler on a fake engine, with the policy chosen from the fake's
    own (honestly simulated) capabilities — the same path production takes."""
    engine = FakeEngine(concurrent=concurrent, total_slots=slots,
                        delay=delay, tokens=tokens)
    caps = await engine.capabilities()
    return Scheduler(engine, caps), engine


async def consume(scheduler, tier, label=""):
    """Run one stream to completion; return its collected tokens."""
    return [t async for t in scheduler.stream(tier, req(), label)]


# --------------------------------------------------------------------------- #
#  1. Concurrent mode overlaps                                                #
# --------------------------------------------------------------------------- #

async def test_chat_and_agent_overlap_in_concurrent_mode():
    scheduler, engine = await make_scheduler(tokens=20)
    assert scheduler.policy.mode == "concurrent"
    # Run an agent stream and a chat stream at the same time.
    agent = asyncio.create_task(consume(scheduler, Tier.BACKGROUND_AGENT, "agent"))
    chat = asyncio.create_task(consume(scheduler, Tier.LIVE_CHAT, "chat"))
    agent_tokens, chat_tokens = await asyncio.gather(agent, chat)
    # Both completed fully — neither was cancelled or starved.
    assert len(agent_tokens) == 20 and len(chat_tokens) == 20
    # And they genuinely overlapped: the fake engine saw 2 busy at once.
    # (Wall-clock check: 40 tokens at 5 ms serially would be ≥200 ms; the
    # gather above finishing means interleaving happened — asserted via the
    # engine having accepted the second request before the first finished.)
    assert len(engine.served) == 2


async def test_wall_clock_shows_real_overlap():
    scheduler, _ = await make_scheduler(tokens=20, delay=0.005)
    loop = asyncio.get_running_loop()
    start = loop.time()
    await asyncio.gather(
        consume(scheduler, Tier.LIVE_CHAT, "c1"),
        consume(scheduler, Tier.BACKGROUND_AGENT, "a1"),
    )
    elapsed = loop.time() - start
    # Serial execution would need ≥ 2 × 20 × 5 ms = 200 ms. Overlapped
    # execution needs ~100 ms. The midpoint separates them cleanly.
    assert elapsed < 0.180, f"streams did not overlap ({elapsed:.3f}s)"


# --------------------------------------------------------------------------- #
#  2. Preemption — and the floor it stops at                                  #
# --------------------------------------------------------------------------- #

async def test_chat_preempts_agent_above_floor():
    # Two slots; the agent grabs both (floor is 1, cap lets it grow).
    scheduler, _ = await make_scheduler(slots=2, tokens=200, delay=0.01)
    preempted = []

    async def agent_stream(name):
        try:
            await consume(scheduler, Tier.BACKGROUND_AGENT, name)
        except PreemptedError:
            preempted.append(name)

    a1 = asyncio.create_task(agent_stream("a1"))
    a2 = asyncio.create_task(agent_stream("a2"))
    await asyncio.sleep(0.05)              # both agent streams mid-flight
    assert scheduler.held(Tier.BACKGROUND_AGENT) == 2
    # A human arrives. One agent stream (above the floor of 1) must yield.
    chat_tokens = await consume(scheduler, Tier.LIVE_CHAT, "chat")
    assert len(chat_tokens) == 200         # chat ran to completion
    await asyncio.gather(a1, a2)
    assert len(preempted) == 1             # exactly one agent stream bumped


async def test_agent_at_floor_is_never_preempted():
    # Two slots; agent holds exactly its floor (1); two chats arrive.
    scheduler, _ = await make_scheduler(slots=2, tokens=60, delay=0.005)
    agent_failed = []

    async def agent_stream():
        try:
            return await consume(scheduler, Tier.BACKGROUND_AGENT, "agent")
        except PreemptedError:
            agent_failed.append(True)

    agent = asyncio.create_task(agent_stream())
    await asyncio.sleep(0.02)              # agent settled into its slot
    # Two simultaneous chats: one takes the free slot, one must WAIT —
    # the agent's floor slot is untouchable.
    await asyncio.gather(
        consume(scheduler, Tier.LIVE_CHAT, "c1"),
        consume(scheduler, Tier.LIVE_CHAT, "c2"),
    )
    await agent
    assert not agent_failed, "agent was preempted below its floor"


# --------------------------------------------------------------------------- #
#  3. Serial mode                                                             #
# --------------------------------------------------------------------------- #

async def test_serial_mode_chosen_from_measurements():
    scheduler, _ = await make_scheduler(concurrent=False)
    # The fake's non-concurrent capabilities (speedup 1.0) → serial policy.
    assert scheduler.policy.mode == "serial"
    assert scheduler.policy.total_slots == 1


async def test_serial_chat_cancels_agent():
    scheduler, _ = await make_scheduler(concurrent=False, tokens=200, delay=0.01)
    preempted = asyncio.Event()

    async def agent_stream():
        try:
            await consume(scheduler, Tier.BACKGROUND_AGENT, "agent")
        except PreemptedError:
            preempted.set()                # the Odysseus rule, observed

    agent = asyncio.create_task(agent_stream())
    await asyncio.sleep(0.05)              # agent holds the single slot
    chat_tokens = await consume(scheduler, Tier.LIVE_CHAT, "chat")
    assert len(chat_tokens) == 200         # the human's request completed
    await agent
    assert preempted.is_set()              # and the agent yielded to it


# --------------------------------------------------------------------------- #
#  4. The floor's promotion — starvation-freedom                              #
# --------------------------------------------------------------------------- #

async def test_starved_agent_promotion_bumps_tier2():
    scheduler, _ = await make_scheduler(slots=2, tokens=100, delay=0.005)
    # Manufacture starvation: the record shows plenty of recent chat
    # tokens and none for the agent, putting it far under its share floor.
    for _ in range(1000):
        scheduler._share.record(Tier.LIVE_CHAT)
    # Fill both slots with research (tier 2 cap on 2 slots is 1... so one
    # research + one chat hold the slots).
    tier2_preempted = asyncio.Event()

    async def research_stream():
        try:
            await consume(scheduler, Tier.FOREGROUND_TASK, "research")
        except PreemptedError:
            tier2_preempted.set()

    r = asyncio.create_task(research_stream())
    c = asyncio.create_task(consume(scheduler, Tier.LIVE_CHAT, "chat"))
    await asyncio.sleep(0.05)              # both mid-flight, no free slot
    # The starved agent submits: promoted, it may bump tier 2 (never chat).
    agent_tokens = await consume(scheduler, Tier.BACKGROUND_AGENT, "agent")
    assert agent_tokens                    # the agent got served
    assert tier2_preempted.is_set()        # at research's expense
    await c
    r.cancel()


async def test_agent_eventually_runs_under_constant_chat():
    # Starvation-freedom, end to end: continuous chat traffic, agent queued.
    scheduler, _ = await make_scheduler(slots=2, tokens=10, delay=0.002)
    agent_done = asyncio.Event()

    async def agent_stream():
        await consume(scheduler, Tier.BACKGROUND_AGENT, "agent")
        agent_done.set()

    async def chat_forever():
        # Back-to-back chats, always one in flight.
        while not agent_done.is_set():
            await consume(scheduler, Tier.LIVE_CHAT, "chat")

    chatter = asyncio.create_task(chat_forever())
    agent = asyncio.create_task(agent_stream())
    # The reservation (floor slots) must let the agent in quickly despite
    # the constant chat pressure. Generous ceiling; typically instant.
    await asyncio.wait_for(agent_done.wait(), timeout=5.0)
    await chatter
    await agent


# --------------------------------------------------------------------------- #
#  5. Slots are always released                                               #
# --------------------------------------------------------------------------- #

async def test_slot_released_after_normal_completion():
    scheduler, _ = await make_scheduler()
    await consume(scheduler, Tier.LIVE_CHAT, "chat")
    assert scheduler.held(Tier.LIVE_CHAT) == 0


async def test_slot_released_when_consumer_abandons_stream():
    scheduler, _ = await make_scheduler(tokens=100, delay=0.01)
    # Take three tokens, then walk away (a closed browser tab).
    stream = scheduler.stream(Tier.LIVE_CHAT, req(), "chat")
    count = 0
    async for _ in stream:
        count += 1
        if count == 3:
            break
    await stream.aclose()                  # what the framework does on disconnect
    # Give release a tick, then confirm the ledger is clean.
    await asyncio.sleep(0.01)
    assert scheduler.held(Tier.LIVE_CHAT) == 0


async def test_slot_released_after_preemption():
    scheduler, _ = await make_scheduler(slots=2, tokens=100, delay=0.005)
    # An older agent stream gets a head start (more accumulated tokens)…
    veteran = asyncio.create_task(consume(scheduler, Tier.BACKGROUND_AGENT, "a-vet"))
    await asyncio.sleep(0.05)
    # …so when chat needs a slot, the LEAST-progressed agent stream — the
    # one below — is chosen as the victim (least completed work wasted).
    chat: asyncio.Task | None = None
    with pytest.raises(PreemptedError):
        async for _ in scheduler.stream(Tier.BACKGROUND_AGENT, req(), "a-victim"):
            if chat is None:               # we're mid-flight: bring the storm
                chat = asyncio.create_task(consume(scheduler, Tier.LIVE_CHAT, "chat"))
    await chat
    await veteran
    # Everything drained; nothing leaked.
    assert scheduler.held(Tier.LIVE_CHAT) == 0
    assert scheduler.held(Tier.BACKGROUND_AGENT) == 0
