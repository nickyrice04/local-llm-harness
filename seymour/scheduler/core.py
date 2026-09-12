"""The scheduler core: admit → grant → account → preempt → release.

The mechanism in one paragraph: every generation must be admitted before it
touches the engine. Admission hands back a Ticket (a held slot). When no
slot is free, the request waits in a priority queue — and, where the policy
allows, the scheduler asks a lower-tier stream to stop (preemption). Preempted
streams raise PreemptedError to their consumer, which checkpoints and
resubmits; their freed slot is granted to the front of the queue. Every token
that streams through is counted per tier, and when the background agent's
measured share falls below its floor, the agent's next request is promoted to
the front of everything.

The scheduler never touches llama-server directly — it works entirely through
the EngineAdapter, which is what makes it testable against a fake engine.
"""

# asyncio: locks, events, and the racing of "next token" vs "preempt".
import asyncio
# contextlib.suppress for tidying up cancelled tasks.
import contextlib
# Logging: every admission decision is traceable (guide, Chapter 28's rule:
# you must be able to answer "was that the agent, the model, or the machine?").
import logging
# AsyncIterator for the public stream() signature.
from typing import AsyncIterator, Optional

# The engine seam: the ONLY thing the scheduler talks to.
from seymour.engine.adapter import EngineAdapter, EngineCapabilities, GenerationRequest
# The event bus that keeps the UI (and avatar) honest.
from seymour.events import bus
# Floor accounting.
from seymour.scheduler.accounting import ShareTracker
# Mode rules.
from seymour.scheduler.policy import Policy, choose_policy
# The records and the tiers.
from seymour.scheduler.tiers import (ModelUnloadingError, PreemptedError,
                                     Ticket, Tier, Waiter)

logger = logging.getLogger(__name__)


class Scheduler:
    """Three tiers, two modes, one engine."""

    def __init__(
        self,
        engine: EngineAdapter,
        caps: EngineCapabilities,
        policy: Optional[Policy] = None,   # tests inject a policy directly
    ) -> None:
        self._engine = engine
        self._caps = caps
        # The policy is chosen ONCE from measured capabilities and then
        # frozen — mode does not flap at runtime.
        self.policy = policy or choose_policy(caps)
        # The ledger: every in-flight generation's ticket…
        self._tickets: set[Ticket] = set()
        # …and every request still waiting for a slot.
        self._waiters: list[Waiter] = []
        # One lock guards the ledger. All admission/release decisions happen
        # under it, so the ledger is always internally consistent.
        self._lock = asyncio.Lock()
        # The measured token shares that make the agent's floor real.
        self._share = ShareTracker()
        # DRAINING (bug 4.2): while True, admission refuses new work so
        # an unload can reach refcount zero. In-flight work is untouched.
        self._draining = False
        # Set exactly when the ledger is empty (no tickets, no waiters) —
        # the unload's "refcount is zero" signal. Starts set: a fresh
        # scheduler is idle.
        self._idle = asyncio.Event()
        self._idle.set()
        logger.info(
            "scheduler up: mode=%s slots=%d (speedup %.2fx measured)",
            self.policy.mode, self.policy.total_slots, caps.measured_speedup,
        )

    # ------------------------------------------------------------------ info
    def held(self, tier: Tier) -> int:
        """How many slots `tier` holds right now (preempting ones included —
        they still occupy a slot until their stream actually ends)."""
        return sum(1 for t in self._tickets if t.tier == tier)

    def snapshot(self) -> dict:
        """The scheduler's live state, for the status UI and the event bus.

        This dict is the honesty layer's raw material: the mode banner, the
        per-tier slot counts, and the avatar all render from it.
        """
        return {
            "mode": self.policy.mode,
            "slots_total": self.policy.total_slots,
            # True while an unload is draining: the UI shows "unloading —
            # waiting for work" instead of pretending all is normal.
            "draining": self._draining,
            "held": {t.name.lower(): self.held(t) for t in Tier},
            "waiting": {t.name.lower(): sum(1 for w in self._waiters if w.tier == t) for t in Tier},
            "shares": self._share.snapshot(),
            "rates": self._share.rates(),
            "agent_floor_share": self.policy.agent_floor_share,
            # Every in-flight stream, oldest first: who holds a slot right
            # now, which pipeline phase it's in, and how fast it's moving.
            # Phase is derived honestly: no visible tokens yet = the
            # engine is still prefilling (or thinking); tokens flowing =
            # decode. The health panel renders this as the slot queue.
            "active": [
                {"label": t.label, "tier": t.tier.name.lower(),
                 "tokens": t.tokens, "tps": t.tps,
                 "phase": "prefill" if t.tokens == 0 else "decode"}
                for t in sorted(self._tickets, key=lambda t: t.seq)
            ],
            # And everything WAITING for a slot, in the order it would be
            # served — the dashed-line section under the four slots.
            "queued": [
                {"label": w.label, "tier": w.tier.name.lower()}
                for w in sorted(self._waiters, key=lambda w: w.priority_key)
            ],
        }

    def _publish(self, type: str, **data) -> None:
        """Publish a scheduler event with the full snapshot attached.

        Every ledger mutation publishes, so this is also where the idle
        flag is kept honest: set exactly when nothing holds or awaits a
        slot (the unload's refcount-zero condition).
        """
        if not self._tickets and not self._waiters:
            self._idle.set()
        else:
            self._idle.clear()
        bus.publish("scheduler", type, snapshot=self.snapshot(), **data)

    # ------------------------------------------------------------- draining
    def begin_drain(self) -> None:
        """Refuse all NEW admissions (in-flight work keeps its slots)."""
        self._draining = True
        self._publish("drain_started")

    def end_drain(self) -> None:
        """The unload was cancelled: admissions flow again."""
        self._draining = False
        self._publish("drain_cancelled")

    def abort_active(self) -> int:
        """Cancel every in-flight and queued consumer (cancel-and-unload).

        Cancelling a consumer task unwinds its stream() frame: the finally
        there closes the engine's HTTP stream — which is what actually
        makes llama-server stop decoding and free the slot — and releases
        the ticket. Queued waiters unwind through _unadmit the same way.
        Returns how many tasks were told to stop.
        """
        me = asyncio.current_task()
        count = 0
        for record in [*self._tickets, *self._waiters]:
            task = record.task
            if task is not None and task is not me and not task.done():
                task.cancel()
                count += 1
        return count

    async def wait_idle(self, timeout: Optional[float] = None) -> bool:
        """Wait for the ledger to empty (refcount zero). True on idle;
        False when the timeout expired with work still live."""
        try:
            await asyncio.wait_for(self._idle.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ------------------------------------------------------------- admission
    async def _admit(self, tier: Tier, label: str) -> Ticket:
        """Wait until `tier` may hold a slot; return the granted ticket."""
        async with self._lock:
            # The drain gate: an unload in progress admits nothing new.
            # Raised INSIDE the lock so a drain begun a microsecond ago
            # can never race a fresh admission past it.
            if self._draining:
                raise ModelUnloadingError(
                    "the model is unloading — no new work is being "
                    "admitted (cancel the unload to continue)")
            waiter = Waiter(tier=tier, label=label,
                            task=asyncio.current_task())
            # The floor promotion (guide, Chapter 3 §3.3): if the agent's
            # measured share of recent tokens has fallen below its floor,
            # its next request jumps the whole queue until it recovers.
            if (
                tier is Tier.BACKGROUND_AGENT
                and self._share.share(tier) < self.policy.agent_floor_share
            ):
                waiter.promoted = True
                logger.info("agent under floor (%.0f%% < %.0f%%): promoting %s",
                            self._share.share(tier) * 100,
                            self.policy.agent_floor_share * 100, label)
            self._waiters.append(waiter)
            self._publish("queued", tier=tier.name.lower(), label=label)
            # Try to grant immediately — the common, uncontended case.
            self._pump_locked()
        # Wait outside the lock: the grant arrives via the waiter's event.
        try:
            await waiter.grant.wait()
        except BaseException:
            # The consumer died while queued — a chat tab closed, the agent
            # was paused, the app is shutting down. Without cleanup the dead
            # waiter stays in the ledger forever: it keeps floor slots
            # reserved, gets preempted-for, and (worst) can be GRANTED a
            # ticket nobody will ever release — a permanently lost slot.
            # The cleanup runs in its own shielded task so that a second
            # cancellation arriving mid-cleanup cannot leave it half-done.
            cleanup = asyncio.create_task(self._unadmit(waiter))
            with contextlib.suppress(BaseException):
                await asyncio.shield(cleanup)
            raise
        # _pump_locked attached the ticket to the waiter when it granted.
        return waiter.ticket        # type: ignore[attr-defined]

    async def _unadmit(self, waiter: Waiter) -> None:
        """Erase a cancelled waiter from the ledger, wherever it got to.

        Two possible states, both handled under the lock: still queued
        (remove it), or already granted in the cancellation race window
        (release the ticket nobody will ever consume). Either way the
        queue is re-pumped — removing agent demand can unreserve floor
        slots, and a returned ticket can seat the next waiter.
        """
        async with self._lock:
            if waiter in self._waiters:
                self._waiters.remove(waiter)
            # The grant race: _grant_locked may have attached a ticket at
            # the same moment the consumer was cancelled.
            ticket = getattr(waiter, "ticket", None)
            if ticket is not None:
                self._tickets.discard(ticket)
            self._pump_locked()
            self._publish("abandoned", tier=waiter.tier.name.lower(),
                          label=waiter.label)

    def _pump_locked(self) -> None:
        """Grant free slots to eligible waiters; trigger preemptions for the
        rest. Called under the lock after every arrival and every release.

        This one function IS the scheduling policy in motion. Everything it
        consults comes from the Policy record, so the difference between
        concurrent and serial mode is entirely in the numbers it reads.
        """
        # Serve the queue in priority order: promoted first, then by tier,
        # then first-come-first-served within a tier.
        self._waiters.sort(key=lambda w: w.priority_key)
        # Slots nobody holds right now…
        free = self.policy.total_slots - len(self._tickets)
        # …plus slots already on their way back (preemption asked, stream
        # not yet closed). Counting these stops a second waiter from
        # preempting ANOTHER victim for the same future slot.
        vacating = sum(1 for t in self._tickets if t.preempt.is_set())

        for waiter in list(self._waiters):
            if free > 0 and self._may_take_locked(waiter, free):
                self._grant_locked(waiter)
                free -= 1
            elif vacating > 0:
                # A slot is already being vacated; this waiter's grant will
                # arrive when that stream closes. No new victim needed.
                vacating -= 1
            else:
                # No slot free, none on the way: may this waiter evict one?
                # First: never evict on behalf of a waiter that could not
                # hold the freed slot anyway (its tier is at cap). Without
                # this check a third research request would kill an agent
                # stream, be refused the freed slot, and repeat — killing
                # the agent's progress over and over for nothing.
                if self.held(waiter.tier) >= self.policy.tier_caps[waiter.tier]:
                    continue
                victim = self._pick_victim_locked(waiter)
                if victim is not None:
                    logger.info("preempting %s (tier %d, %d tokens in) for %s",
                                victim.label, victim.tier, victim.tokens, waiter.label)
                    # Ask the stream to stop. Its consumer gets
                    # PreemptedError; release() then re-pumps and the freed
                    # slot goes to the head of the queue.
                    victim.preempt.set()
                    self._publish("preempted", victim=victim.label, for_=waiter.label)

    def _may_take_locked(self, waiter: Waiter, free: int) -> bool:
        """May `waiter` take one of the `free` slots right now?"""
        # Rule 1: a tier never exceeds its cap on held slots.
        if self.held(waiter.tier) >= self.policy.tier_caps[waiter.tier]:
            return False
        # Rule 2 — the floor's other half: while the agent has work QUEUED
        # and holds fewer than its floor slots, that many free slots are
        # reserved for it. A higher tier may only take what's left over.
        if waiter.tier is not Tier.BACKGROUND_AGENT and self.policy.agent_floor_slots > 0:
            agent_demand = any(w.tier is Tier.BACKGROUND_AGENT for w in self._waiters)
            if agent_demand:
                reserved = max(0, self.policy.agent_floor_slots - self.held(Tier.BACKGROUND_AGENT))
                if free <= reserved:
                    return False
        return True

    def _grant_locked(self, waiter: Waiter) -> None:
        """Turn a waiter into a ticket holding one slot, and wake it."""
        ticket = Ticket(tier=waiter.tier, label=waiter.label)
        self._tickets.add(ticket)
        self._waiters.remove(waiter)
        # Hand the ticket over on the waiter object, then set the event —
        # the awaiting _admit() picks it up immediately after.
        waiter.ticket = ticket          # type: ignore[attr-defined]
        waiter.grant.set()
        self._publish("granted", tier=waiter.tier.name.lower(), label=waiter.label)

    def _pick_victim_locked(self, waiter: Waiter) -> Optional[Ticket]:
        """Choose one in-flight stream for `waiter` to evict, or None.

        Victim tiers come from the policy table. Two extra rules:
        - The agent is never evicted below its floor slots (that IS the floor).
        - A promoted agent waiter may bump research (tier 2) — 'promoted
          above everything' — but never live chat: cancelling a stream a
          human is watching is never acceptable.
        """
        # Which tiers may this waiter evict?
        if waiter.promoted and waiter.tier is Tier.BACKGROUND_AGENT:
            victim_tiers = [Tier.FOREGROUND_TASK]
        else:
            victim_tiers = self.policy.preempts.get(waiter.tier, [])

        for vt in victim_tiers:
            # Candidates: streams in the victim tier not already stopping.
            candidates = [t for t in self._tickets if t.tier is vt and not t.preempt.is_set()]
            # Floor protection: never evict the agent below its floor.
            if vt is Tier.BACKGROUND_AGENT:
                evictable = len(candidates) - self.policy.agent_floor_slots
                if evictable <= 0:
                    continue
                candidates = candidates[:]  # all are candidates; count-capped below
            if not candidates:
                continue
            # Evict the stream with the LEAST progress: it has the least
            # completed work to throw away and re-do.
            return min(candidates, key=lambda t: t.tokens)
        return None

    async def _release(self, ticket: Ticket) -> None:
        """Give a slot back and serve the queue."""
        async with self._lock:
            self._tickets.discard(ticket)
            self._pump_locked()
            self._publish("released", tier=ticket.tier.name.lower(), label=ticket.label)

    # ------------------------------------------------------------ generation
    async def stream(
        self,
        tier: Tier,
        req: GenerationRequest,
        label: str = "",
    ) -> AsyncIterator[str]:
        """The scheduler's main entrance: admit, then relay tokens.

        Yields token deltas exactly like the engine does, with two additions:
        every delta is counted for floor accounting, and if the scheduler
        preempts this stream, the consumer receives PreemptedError (after
        the underlying generation has been aborted and its slot freed).
        """
        ticket = await self._admit(tier, label)
        # The consumer task now DRIVES this ticket — recorded so a
        # cancel-and-unload can abort the generation for real.
        ticket.task = asyncio.current_task()
        # A task that completes when (if) the scheduler asks us to stop.
        preempt_wait = asyncio.create_task(ticket.preempt.wait())
        # The engine's token stream for this request.
        inner = self._engine.stream(req).__aiter__()
        # The in-flight "fetch the next token" task. Held OUTSIDE the loop
        # so the finally block can cancel it even when the consumer abandons
        # us mid-yield (a browser tab closing raises GeneratorExit at the
        # yield, and an un-cancelled fetch would orphan the engine stream).
        next_tok: Optional[asyncio.Task] = None
        try:
            while True:
                # Race the next token against the preemption signal.
                next_tok = asyncio.create_task(inner.__anext__())
                done, _ = await asyncio.wait(
                    {next_tok, preempt_wait},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if preempt_wait in done:
                    # Preempted: abort the in-flight token fetch. Cancelling
                    # it closes the adapter's HTTP stream, which is what
                    # tells llama-server to stop generating and free the slot.
                    next_tok.cancel()
                    with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration, Exception):
                        await next_tok
                    raise PreemptedError(f"{ticket.label or 'stream'} preempted")
                try:
                    token = next_tok.result()
                except StopAsyncIteration:
                    return          # normal end of generation
                # Account the token: progress on the ticket (victim choice)
                # and the tier share (floor accounting).
                ticket.tokens += 1
                self._share.record(tier)
                yield token
        finally:
            # Runs on normal end, preemption, consumer disconnect, or error:
            # stop the token fetch, close the engine stream, and give the
            # slot back — ALWAYS, in that order (aclose() would raise if the
            # generator still had a running __anext__).
            preempt_wait.cancel()
            if next_tok is not None and not next_tok.done():
                next_tok.cancel()
                with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration, Exception):
                    await next_tok
            with contextlib.suppress(Exception):
                await inner.aclose()
            await self._release(ticket)

    async def complete(self, tier: Tier, req: GenerationRequest, label: str = "") -> str:
        """Non-streaming convenience: join the stream into one string.

        PreemptedError passes through to the caller — an agent step that gets
        preempted mid-thought must know, so it can checkpoint and retry.
        """
        parts: list[str] = []
        async for token in self.stream(tier, req, label):
            parts.append(token)
        return "".join(parts)
