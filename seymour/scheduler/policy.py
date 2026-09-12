"""The two operating modes, expressed as data + small pure functions.

The startup handshake measures what the engine can actually do; this module
turns that measurement into a policy choice (guide, Chapter 3 §3.4):

    CONCURRENT — batching confirmed. N slots, three tiers, the agent has a
                 floor, live chat preempts a lower tier when no slot is free.
    SERIAL     — batching absent or unconvincing. One request at a time,
                 FIFO by priority, and any foreground request preempts the
                 agent (Odysseus's design — correct when concurrency is not
                 available, and still useful).

Serial mode is a SUPPORTED CONFIGURATION, not a failure state, and the UI
always says which mode is live and what it means for the user.

Keeping the rules here — separate from the core's mechanics — means the
difference between modes is visible on one screen instead of being smeared
through the admission logic as if-statements.
"""

# Dataclass for the frozen policy record.
from dataclasses import dataclass

# The measured engine capabilities the choice is based on.
from seymour.engine.adapter import EngineCapabilities
# Settings: thresholds and caps come from configuration, not constants.
from seymour.config import settings
# The tiers the rules speak about.
from seymour.scheduler.tiers import Tier


@dataclass(frozen=True)
class Policy:
    """Everything mode-specific, in one immutable record."""

    mode: str                     # "concurrent" | "serial" — for display
    total_slots: int              # how many generations may be in flight
    tier_caps: dict               # Tier → max slots that tier may HOLD
    agent_floor_slots: int        # slots reserved for the agent when it has
                                  # work queued (0 in serial mode: you cannot
                                  # reserve a share of a single slot)
    agent_floor_share: float      # token-share line below which the agent's
                                  # next request is promoted
    # Which tiers a given tier may preempt when it needs a slot and none is
    # free. Order matters: victims are tried left to right.
    preempts: dict


def choose_policy(caps: EngineCapabilities) -> Policy:
    """Turn measured capabilities into the mode Seymour will run in.

    The decision reads ONLY measurements (probe results), never the model
    name — the rule the whole guide keeps returning to.
    """
    # Concurrent mode needs real overlap and more than one slot. The
    # OVERLAP VERDICT belongs to the handshake (probe 4), which watched
    # slots actually run side by side; re-deriving it from the throughput
    # ratio here would second-guess a direct observation with a proxy —
    # and did, measurably: with MTP drafting on, three slots demonstrably
    # ran at once for a 1.35x aggregate gain, the ratio test rejected it,
    # and a 4-slot engine collapsed to one. Throughput is not the point;
    # a chat that starts now instead of queueing behind the agent is.
    concurrent_ok = caps.concurrent and caps.total_slots >= 2

    if concurrent_ok:
        return Policy(
            mode="concurrent",
            total_slots=caps.total_slots,
            tier_caps={
                # Live chat may use every slot — bursts of quick questions
                # should never queue behind an artificial cap.
                Tier.LIVE_CHAT: caps.total_slots,
                # Research and friends are capped so a fan-out job cannot
                # take the whole machine (that would rebuild the trap).
                Tier.FOREGROUND_TASK: min(settings.tier2_max_slots, caps.total_slots - 1),
                # The agent may GROW into every free slot when the machine
                # is otherwise idle — that idle capacity is the whole point
                # of one instance instead of two.
                Tier.BACKGROUND_AGENT: caps.total_slots,
            },
            agent_floor_slots=min(settings.agent_floor_slots, caps.total_slots - 1),
            agent_floor_share=settings.agent_floor_share,
            preempts={
                # Chat first empties the agent down to its floor, then
                # bumps research. It never waits behind either.
                Tier.LIVE_CHAT: [Tier.BACKGROUND_AGENT, Tier.FOREGROUND_TASK],
                # Research may shrink the agent toward its floor, never chat.
                Tier.FOREGROUND_TASK: [Tier.BACKGROUND_AGENT],
                # The agent preempts nobody — except that when it is under
                # its floor, core.py lets its PROMOTED request bump research.
                Tier.BACKGROUND_AGENT: [],
            },
        )

    # Serial mode: the honest fallback.
    return Policy(
        mode="serial",
        total_slots=1,
        # Every tier is capped at the single slot.
        tier_caps={t: 1 for t in Tier},
        # A floor measured in slots is meaningless with one slot…
        agent_floor_slots=0,
        # …but the SHARE floor still applies: if the agent got starved of
        # tokens for a whole window while it had work, its next request is
        # promoted to the front of the queue (served, then chat resumes).
        agent_floor_share=settings.agent_floor_share,
        preempts={
            # This is exactly Odysseus's rule: the human takes the pipe and
            # the background work is cancelled (we checkpoint + resume it).
            Tier.LIVE_CHAT: [Tier.BACKGROUND_AGENT, Tier.FOREGROUND_TASK],
            Tier.FOREGROUND_TASK: [Tier.BACKGROUND_AGENT],
            Tier.BACKGROUND_AGENT: [],
        },
    )
