"""The event bus: how the backend tells the UI what is really happening.

One in-process publish/subscribe hub. Backend components publish small typed
events ("scheduler state changed", "agent started a step", "download at 40%"),
and routes/events.py relays them to the browser over a single SSE stream.

This is the honesty layer's plumbing. The avatar in the corner of the UI is
driven ONLY by events that real components published at real transitions —
never by a timer, never by guesswork (the Papert rule from the guide's
Chapter 25: the character on screen must reflect the machine's actual state).
"""

# asyncio gives us Queue — the mailbox each subscriber reads from.
import asyncio
# dataclass for a small, typed event record.
from dataclasses import dataclass, field
# time for the event timestamp (wall clock, for the UI's benefit).
import time
# Any because event payloads are small free-form dicts.
from typing import Any


@dataclass(frozen=True)
class Event:
    """One thing that happened, as told to the UI."""

    topic: str                      # coarse channel: "scheduler" | "agent" |
                                    # "engine" | "download" | "chat" | "memory"
    type: str                       # what happened, e.g. "mode_changed"
    data: dict[str, Any] = field(default_factory=dict)  # the details
    ts: float = field(default_factory=time.time)        # when (unix seconds)


class EventBus:
    """A minimal fan-out hub: publish() copies the event to every subscriber.

    Each subscriber owns an asyncio.Queue. Queues are bounded so a stuck
    browser tab cannot make the backend hoard events forever — when a queue
    is full we drop the OLDEST event, because for live status the newest
    event is always the one that matters.
    """

    def __init__(self) -> None:
        # The set of live subscriber queues. A set, so unsubscribe is O(1).
        self._queues: set[asyncio.Queue[Event]] = set()

    def subscribe(self) -> asyncio.Queue[Event]:
        """Register a new listener and hand it its private queue."""
        # maxsize=256: plenty for a UI, small enough to bound memory.
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=256)
        self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        """Remove a listener (called when its SSE connection closes)."""
        # discard, not remove: unsubscribing twice is harmless.
        self._queues.discard(q)

    def subscribers(self) -> int:
        """How many live listeners (open /api/events streams) there are —
        a tool that needs a browser (check_page) asks before it waits."""
        return len(self._queues)

    def publish(self, topic: str, type: str, **data: Any) -> None:
        """Send an event to every current subscriber. Fire-and-forget:
        publishing never blocks and never raises on a slow consumer."""
        event = Event(topic=topic, type=type, data=data)
        for q in self._queues:
            try:
                # Non-blocking put; the queue is bounded (see class docstring).
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Drop the oldest event to make room for the newest one.
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass          # raced with the consumer — fine either way
                q.put_nowait(event)


# The single shared bus, imported everywhere as `from seymour.events import bus`.
bus = EventBus()
