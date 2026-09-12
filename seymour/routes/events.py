"""The event stream: one SSE connection carrying every live update.

The browser opens GET /api/events once and receives everything the backend
publishes — scheduler transitions, agent steps, download progress, engine
lifecycle — as typed JSON frames:

    data: {"topic": "scheduler", "type": "granted", "data": {…}, "ts": …}

Comment heartbeats (`: heartbeat`) flow every 15 s of silence so proxies
and browsers never drop an idle connection (a llama.cpp-serving lesson from
the reference implementation).
"""

import asyncio
import json

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from seymour.events import bus

router = APIRouter(prefix="/api")


@router.get("/events")
async def events():
    """Subscribe this client to the bus and relay until it disconnects."""
    queue = bus.subscribe()

    async def relay():
        """Queue → SSE frames, with heartbeats during quiet stretches."""
        heartbeats = 0
        try:
            while True:
                try:
                    # Wait for the next event, but never silently forever.
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # Quiet period: send a comment frame. Browsers ignore
                    # it; proxy idle timers reset. Cheap insurance.
                    heartbeats += 1
                    yield f": heartbeat {heartbeats}\n\n"
                    continue
                # One event, one frame; json.dumps handles all escaping.
                yield "data: " + json.dumps({
                    "topic": event.topic,
                    "type": event.type,
                    "data": event.data,
                    "ts": event.ts,
                }) + "\n\n"
        finally:
            # Disconnect (or server shutdown): stop copying events to a
            # queue nobody reads.
            bus.unsubscribe(queue)

    return StreamingResponse(
        relay(),
        media_type="text/event-stream",
        # The same anti-buffering headers the chat stream needs.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
