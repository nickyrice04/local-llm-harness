"""Floor accounting: measuring each tier's share of recent decode tokens.

A floor is not a priority (guide, Chapter 3 §3.3). Priority answers "who goes
first when both want to go?" A floor answers "what is the minimum this one
gets, even when it always loses?" The agent is lowest priority AND has a
floor, and those don't conflict: the floor is enforced by measuring the
agent's actual share of recent work and promoting its next request when the
share falls below the line.

This module is the measuring half. It counts decode tokens per tier in small
time buckets and reports each tier's share over a sliding window.
"""

# monotonic clock — immune to wall-clock changes mid-measurement.
import time
# deque: an efficient bounded list of recent buckets.
from collections import deque

# The Tier enum the counts are keyed by.
from seymour.scheduler.tiers import Tier


class ShareTracker:
    """Token counts per tier over a sliding window of time buckets.

    Buckets rather than raw events: recording a token is O(1) with no
    allocation, and expiry is 'drop old buckets' rather than scanning a list
    of timestamps. At a few thousand tokens/minute this is effectively free.
    """

    def __init__(self, window_s: float = 60.0, bucket_s: float = 5.0) -> None:
        self._window_s = window_s          # how much history counts
        self._bucket_s = bucket_s          # granularity of that history
        # Each bucket: [start_time, {tier: token_count}].
        self._buckets: deque[list] = deque()

    def _current_bucket(self) -> dict:
        """Return the live bucket's counts dict, rolling to a new bucket (and
        expiring old ones) as time passes."""
        now = time.monotonic()
        # Start a new bucket if there is none, or the newest one is stale.
        if not self._buckets or now - self._buckets[-1][0] >= self._bucket_s:
            self._buckets.append([now, {t: 0 for t in Tier}])
        # Expire buckets that have slid out of the window.
        while self._buckets and now - self._buckets[0][0] > self._window_s:
            self._buckets.popleft()
        return self._buckets[-1][1]

    def record(self, tier: Tier, tokens: int = 1) -> None:
        """Count `tokens` decode tokens against `tier`, in the live bucket."""
        self._current_bucket()[tier] += tokens

    def share(self, tier: Tier) -> float:
        """`tier`'s fraction of all tokens in the window.

        Returns 1.0 when NO tokens were produced at all: an idle system
        starves nobody, so an idle window must not read as 'under the floor'
        and trigger a spurious promotion.
        """
        self._current_bucket()               # roll/expire first
        # Sum every bucket's counts into per-tier totals.
        totals = {t: 0 for t in Tier}
        for _, counts in self._buckets:
            for t, n in counts.items():
                totals[t] += n
        all_tokens = sum(totals.values())
        # The idle case described in the docstring.
        if all_tokens == 0:
            return 1.0
        return totals[tier] / all_tokens

    def snapshot(self) -> dict:
        """All three shares at once, for the status UI and the event bus."""
        return {t.name.lower(): round(self.share(t), 3) for t in Tier}

    def rates(self) -> dict:
        """Each tier's decode rate in tokens/second over the live window.

        This is the header's honest throughput meter: it reports what the
        whole system is producing per tier RIGHT NOW (well, over the last
        minute) — not a single stream's speed, which the chat view reports
        separately from the engine's own timings.
        """
        self._current_bucket()               # roll/expire first
        if not self._buckets:
            return {t.name.lower(): 0.0 for t in Tier}
        # Sum every bucket's counts into per-tier totals.
        totals = {t: 0 for t in Tier}
        for _, counts in self._buckets:
            for t, n in counts.items():
                totals[t] += n
        # Divide by the time the window actually covers (the buckets span
        # less than window_s right after startup — dividing by the full
        # window would understate the rate exactly when it's most watched).
        covered = max(
            self._bucket_s,
            time.monotonic() - self._buckets[0][0],
        )
        return {t.name.lower(): round(totals[t] / covered, 1) for t in Tier}
