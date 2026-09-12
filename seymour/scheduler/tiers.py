"""The three priority tiers, and the small records the scheduler works with.

Why three tiers and not two (guide, Chapter 3 §3.3): a two-tier human/agent
split has a trap. Start a twenty-minute deep-research run — you started it,
so it's "foreground" — then ask a quick question, and your question queues
behind it in the same tier. The sluggish laptop, rebuilt using the fix.

The classification that matters is not "who started it" but "is a human
watching tokens appear right now?"
"""

# IntEnum so tiers order naturally: Tier.LIVE_CHAT < Tier.FOREGROUND_TASK.
from enum import IntEnum
# Dataclasses for the ticket/waiter records; field for mutable defaults.
from dataclasses import dataclass, field
# asyncio primitives used inside the records.
import asyncio
# itertools.count hands out monotonically increasing sequence numbers.
import itertools


class Tier(IntEnum):
    """The three priority tiers. Lower value = higher priority."""

    # A human is watching a token stream, right now. Always wins: guaranteed
    # a slot immediately, by preempting a lower tier if none is free.
    LIVE_CHAT = 1
    # You started it and want it soon, but aren't watching each token (deep
    # research, "summarize these 40 documents"). Yields to live chat;
    # outranks the background agent; capped so a fan-out can't take the
    # whole machine.
    FOREGROUND_TASK = 2
    # The always-on primary agent. Lowest priority — AND a guaranteed
    # minimum floor, because it is the only tier that would otherwise be
    # starved to zero indefinitely.
    BACKGROUND_AGENT = 3


class PreemptedError(Exception):
    """Raised INTO a stream's consumer when the scheduler cancels it.

    Callers in tiers 2 and 3 catch this, checkpoint their progress, and
    resubmit — that is the contract that makes preemption safe. Tier 1 is
    never preempted, so chat code never sees this.
    """


class ModelUnloadingError(RuntimeError):
    """Raised by admission while the scheduler is DRAINING for an unload.

    The contract (bug 4.2): the moment an unload begins, no NEW work is
    admitted — in-flight generations finish (drain) or are cancelled
    (cancel-and-unload), and the engine only stops at refcount zero.
    Loop consumers treat this as "pause and wait for a model", never as
    a step failure to retry — retrying against a draining scheduler
    would just hold the unload hostage.
    """


# A process-wide sequence for FIFO ordering within a tier. itertools.count
# is not thread-safe in general but the scheduler only touches it from the
# event loop, which is single-threaded.
_seq = itertools.count()


# time.monotonic stamps a ticket's admission for its live tok/s readout.
import time


# eq=False keeps Python's default identity semantics: two tickets are the
# same ticket only if they ARE the same object — which also makes tickets
# hashable, so the scheduler can hold them in a set.
@dataclass(eq=False)
class Ticket:
    """One admitted, in-flight generation: the scheduler's handle on it."""

    tier: Tier                      # which tier admitted it
    label: str                      # human-readable, e.g. "chat:4f2a" — for
                                    # logs and the status UI, never for logic
    seq: int = field(default_factory=lambda: next(_seq))  # admission order
    tokens: int = 0                 # decode tokens streamed so far (progress)
    # When this stream was admitted — with `tokens`, this is the honest
    # live tok/s the health panel shows per running stream.
    started: float = field(default_factory=time.monotonic)
    # Set by the scheduler to ask this stream to stop. The stream wrapper
    # watches it and raises PreemptedError to its consumer.
    preempt: asyncio.Event = field(default_factory=asyncio.Event)
    # The consumer task driving this stream — captured at admission so a
    # cancel-and-unload can abort every in-flight generation for real
    # (cancelling the consumer closes the engine's HTTP stream, which is
    # what makes llama-server stop decoding and free the slot).
    task: "asyncio.Task | None" = None

    @property
    def tps(self) -> float:
        """Average visible tokens/sec since admission (0 while prefilling)."""
        elapsed = time.monotonic() - self.started
        return round(self.tokens / elapsed, 1) if elapsed > 0.5 else 0.0


# eq=False for the same identity-semantics reason as Ticket.
@dataclass(eq=False)
class Waiter:
    """One request queued for admission, waiting for a slot grant."""

    tier: Tier                      # its tier (order within tier is FIFO)
    label: str                      # same display label as its future ticket
    seq: int = field(default_factory=lambda: next(_seq))  # arrival order
    promoted: bool = False          # True when this is the agent's next
                                    # request while the agent is under its
                                    # floor — promoted waiters are served
                                    # before everything else
    # The scheduler sets this to wake the waiter up with a granted slot.
    grant: asyncio.Event = field(default_factory=asyncio.Event)
    # The consumer task waiting on that grant (same purpose as
    # Ticket.task: cancel-and-unload must be able to abort queued work).
    task: "asyncio.Task | None" = None

    @property
    def priority_key(self) -> tuple:
        """Sort key for the wait queue: promoted first, then tier, then FIFO."""
        return (0 if self.promoted else 1, int(self.tier), self.seq)
