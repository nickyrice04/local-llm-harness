"""The scheduler — Seymour's actual contribution.

Odysseus (the reference implementation this project forks from) solves
model contention by preventing concurrency: a global lock, one background
task at a time, and the background task is CANCELLED whenever the human
chats. Reasonable for a tool that must run on any backend — but it means
the agent makes zero progress while you type.

Seymour ships with a known backend (llama.cpp) whose parallel slots make
real concurrency possible, so this package implements the policy layer that
exploits it:

    tiers.py      — the three priority tiers and the ticket/waiter records
    policy.py     — the rules for each mode (concurrent / serial), as data
    accounting.py — measured decode-token share per tier (the agent's floor)
    core.py       — the Scheduler itself: admit → grant → account → preempt

There are two schedulers in the system and it matters not to confuse them
(guide, Chapter 3 §3.6): llama-server's own scheduler decides which admitted
sequences advance each forward pass; THIS scheduler decides what is admitted
at all, how many slots each tier may hold, and when to cancel a stream. It
works entirely above the HTTP boundary.
"""

# Re-export the public surface so callers write `from seymour.scheduler import …`.
from seymour.scheduler.core import Scheduler          # noqa: F401
from seymour.scheduler.tiers import PreemptedError, Tier  # noqa: F401
