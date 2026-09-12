"""Live runs: a chat run detached from the HTTP response that started it.

Before 2026-09-11 a chat run WAS its SSE response: the route iterated the
executor's generator straight into the wire, so a closed tab (or a view
switch, or a laptop lid) raised GeneratorExit inside the run and the
server recorded it as cancelled. Opening the Code pane had to be built
as a pane, not a view, to dodge exactly that.

Now the run is an asyncio task that drives the generator and FANS OUT
its frames: to every attached consumer (the response that started it,
any tab that re-attaches through /api/runs/{id}/live) and into a bounded
replay buffer so a late arrival sees the run so far. A consumer going
away unsubscribes its queue; the run does not notice. Stopping is an
explicit act — POST /api/runs/{id}/cancel — which cancels the task, and
the executor's own CancelledError path records "stopped by the person"
exactly as before.

The replay buffer coalesces: consecutive deltas merge into one, a
streaming tool call's progress frames fold into one frame carrying the
whole content so far. A run that wrote a 300-line page replays as a
handful of frames, not thousands.
"""

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import AsyncIterator

from seymour.events import bus

logger = logging.getLogger(__name__)

# How long a finished run stays attachable (its replay buffer kept) so a
# tab that reopens the conversation right after the end still sees the
# frames; after this, the persisted message is the record.
LINGER_S = 120.0
# The replay buffer's cap on frames (deltas coalesce, so this is rarely
# approached; it bounds a pathological run).
MAX_FRAMES = 4_000


@dataclass
class LiveRun:
    """One run in flight (or just finished): its task, its consumers and
    its replay buffer."""

    session_id: str
    run_id: str = ""                       # known after the executor's first frame
    task: asyncio.Task | None = None
    frames: list[dict] = field(default_factory=list)
    queues: set[asyncio.Queue] = field(default_factory=set)
    done: bool = False
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def push(self, frame: dict) -> None:
        """Record one frame and hand it to every attached consumer."""
        self._buffer(frame)
        for queue in list(self.queues):
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                # A consumer that stopped reading loses frames; the run
                # must never block on it (the persisted message and the
                # trace are the records of truth, not a queue).
                pass

    def _buffer(self, frame: dict) -> None:
        """Append to the replay buffer, coalescing where it is safe."""
        last = self.frames[-1] if self.frames else None
        if last is not None and "delta" in frame and "delta" in last and len(last) == 1 and len(frame) == 1:
            last["delta"] += frame["delta"]
            return
        if last is not None and "tool_progress" in frame and "tool_progress" in last:
            new, old = frame["tool_progress"], last["tool_progress"]
            if new.get("name") == old.get("name") and new.get("path") == old.get("path") and not new.get("reset"):
                old["delta"] = (old.get("delta") or "") + (new.get("delta") or "")
                for key in ("tail", "chars", "seconds", "field"):
                    if key in new:
                        old[key] = new[key]
                return
        if len(self.frames) >= MAX_FRAMES:
            # Drop the oldest delta-only frame; structural frames stay.
            for i, old in enumerate(self.frames):
                if "delta" in old and len(old) == 1:
                    del self.frames[i]
                    break
            else:
                del self.frames[0]
        self.frames.append(dict(frame))

    def attach(self) -> asyncio.Queue:
        """A fresh consumer queue, subscribed to future frames."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)
        self.queues.add(queue)
        return queue

    def detach(self, queue: asyncio.Queue) -> None:
        self.queues.discard(queue)


# The registries: by run id (once known) and by session (from the start).
_by_run: dict[str, LiveRun] = {}
_by_session: dict[str, LiveRun] = {}


def by_run(run_id: str) -> LiveRun | None:
    return _by_run.get(run_id)


def by_session(session_id: str) -> LiveRun | None:
    """The session's live run, if one is running or just finished."""
    live = _by_session.get(session_id)
    if live is None:
        return None
    if live.done and live.finished_at and time.time() - live.finished_at > LINGER_S:
        _forget(live)
        return None
    return live


def _forget(live: LiveRun) -> None:
    if _by_session.get(live.session_id) is live:
        _by_session.pop(live.session_id, None)
    if live.run_id and _by_run.get(live.run_id) is live:
        _by_run.pop(live.run_id, None)


def start(session_id: str, generator: AsyncIterator[dict]) -> LiveRun:
    """Detach `generator` (the executor's frame stream) into a task that
    fans its frames out. Returns the LiveRun to attach to."""
    live = LiveRun(session_id=session_id)
    _by_session[session_id] = live

    async def drive() -> None:
        try:
            async for frame in generator:
                if "run_id" in frame and not live.run_id:
                    live.run_id = frame["run_id"]
                    _by_run[live.run_id] = live
                    bus.publish("run", "started", run_id=live.run_id, session_id=session_id)
                live.push(frame)
        except asyncio.CancelledError:
            # The explicit cancel: the executor has already recorded the
            # stop; tell the consumers, then let the cancellation stand.
            live.push({"error": "stopped"})
            raise
        except Exception as error:                 # the executor reports its own failures; this is the belt
            logger.exception("live run crashed")
            live.push({"error": f"run failed: {type(error).__name__}"})
        finally:
            live.done = True
            live.finished_at = time.time()
            live.push({"done": True})
            asyncio.get_running_loop().call_later(LINGER_S + 1, _forget, live)

    live.task = asyncio.create_task(drive())
    return live


async def follow(live: LiveRun, replay: bool = True) -> AsyncIterator[dict]:
    """Yield the run's frames — the replay buffer first (when asked),
    then live ones — until it is done. Detaches on the way out, whether
    the consumer finished, disconnected or was cancelled."""
    queue = live.attach()
    try:
        # Everything so far, then whatever arrives. A frame can land in
        # the queue while the replay is being sent; that is fine — the
        # queue was attached BEFORE the replay was read, so nothing is
        # missed, and the replay's coalesced form does not overlap with
        # the queued frames (they were pushed after the snapshot).
        snapshot = [dict(f) for f in live.frames] if replay else []
        for frame in snapshot:
            yield frame
            if frame.get("done"):
                return
        if live.done and not any(f.get("done") for f in snapshot):
            yield {"done": True}
            return
        if live.done:
            return
        while True:
            frame = await queue.get()
            yield frame
            if frame.get("done"):
                return
    finally:
        live.detach(queue)


async def cancel(run_id: str) -> bool:
    """Stop a live run (the explicit route). False when there is nothing
    running under that id — a stale click, not an error."""
    live = _by_run.get(run_id)
    if live is None or live.task is None or live.done:
        return False
    live.task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await live.task
    return True


async def shutdown() -> None:
    """App exit: stop every live run (their partial replies are persisted
    by the executor's own finally)."""
    for live in list(_by_run.values()):
        if live.task and not live.done:
            live.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await live.task
