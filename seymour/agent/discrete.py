"""Discrete agent tasks: chat-started jobs that run NOW and finish.

The primary agent (manager.py) is one continuous seat at Tier 3 — Seymour
grinding away in the background, often open-endedly. THIS module is the
other kind of agentic work: "find the sheet, combine it, highlight the
figures" — a discrete job the user just asked for in chat. It runs at
Tier 2 (FOREGROUND_TASK): the user is loosely waiting, so it outranks the
background agent and yields to live chat, exactly like deep research.

Several may run at once — the scheduler's Tier 2 slot cap is the real
concurrency brake — but a small pool cap here keeps the journal noise and
memory bounded too. Same loop, same tools, same journal as the primary
agent (one visible mechanism, twice applied).
"""

import asyncio
import contextlib
import logging
import uuid
from typing import Optional

from seymour import runtime
from seymour.agent.loop import StepOutcome, journal, run_step
from seymour.db import AgentTask, ChatSession, Message, SessionLocal, utcnow
from seymour.events import bus
from seymour.scheduler.tiers import ModelUnloadingError, PreemptedError, Tier

logger = logging.getLogger(__name__)

# ---- Bounded counters ------------------------------------------------------ #
# Steps per run: a discrete job that hasn't finished in 60 steps pauses for
# review rather than burning tokens (resume continues it).
MAX_STEPS = 60
# How many discrete tasks may RUN at once (more just queue as 'paused'
# would be dishonest — they stay 'queued' until a seat frees).
MAX_CONCURRENT = 3
# Identical consecutive tool calls before pausing (same rule as primary).
REPEAT_PAUSE = 6


class DiscreteTaskManager:
    """The pool of chat-started agent jobs (kind='task', Tier 2)."""

    def __init__(self) -> None:
        # task_id → the asyncio task running its loop.
        self._runners: dict[str, asyncio.Task] = {}
        # Set during shutdown so a finishing runner's pump can't spin up
        # NEW work while the app is closing.
        self._closing = False

    # ------------------------------------------------------------- lifecycle
    async def startup(self) -> None:
        """Boot: anything left 'running' by the last session re-queues —
        discrete jobs are foreground work; silently resuming them without
        the user present would be a surprise, not a feature."""
        with SessionLocal() as db:
            stale = (db.query(AgentTask)
                     .filter_by(status="running", kind="task").all())
            for task in stale:
                task.status = "paused"
            db.commit()
        for task in stale:
            journal(task.id, "status",
                    "Seymour restarted — paused; resume when ready.")

    async def shutdown(self) -> None:
        """App exit: stop every live runner (their rows stay as they are;
        the paused/queued states resume honestly next boot)."""
        self._closing = True
        for runner in list(self._runners.values()):
            if not runner.done():
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await runner

    # ----------------------------------------------------------- public api
    async def create(self, goal: str, session_id: str = "") -> dict:
        """Accept a discrete job from chat; starts immediately if the pool
        has room, else queues (started by the next finisher)."""
        task = AgentTask(id=str(uuid.uuid4()), goal=goal.strip(), kind="task",
                         session_id=session_id)
        with SessionLocal() as db:
            db.add(task)
            db.commit()
        bus.publish("tasks", "created", task_id=task.id, goal=task.goal,
                    session_id=session_id)
        await self._pump()
        return {"id": task.id, "status": self._status_of(task.id)}

    async def pause(self, task_id: str) -> str:
        """Stop the loop; keep every checkpoint."""
        self._require(task_id, ("running", "queued", "paused"))
        self._set_status(task_id, "paused")
        await self._stop_runner(task_id)
        journal(task_id, "status", "Paused by user.")
        bus.publish("tasks", "paused", task_id=task_id)
        await self._pump()               # the freed pool seat serves the queue
        return "paused"

    async def resume(self, task_id: str) -> str:
        """Continue from the checkpoint (queues if the pool is full)."""
        self._require(task_id, ("paused", "blocked", "queued"))
        self._set_status(task_id, "queued")
        journal(task_id, "status", "Resumed.")
        await self._pump()
        return self._status_of(task_id)

    async def cancel(self, task_id: str) -> str:
        """Abandon the job for good (its journal remains readable)."""
        self._require(task_id, ("running", "queued", "paused", "blocked"))
        self._set_status(task_id, "cancelled")
        await self._stop_runner(task_id)
        journal(task_id, "status", "Cancelled by user.")
        bus.publish("tasks", "cancelled", task_id=task_id)
        await self._pump()
        return "cancelled"

    async def respond(self, task_id: str, answer: str) -> str:
        """The user answered an ask_user question: unblock and continue."""
        self._require(task_id, ("blocked",))
        journal(task_id, "approval_response", answer)
        return await self.resume(task_id)

    # ------------------------------------------------------------- internals
    def _require(self, task_id: str, allowed: tuple[str, ...]) -> str:
        """Same contract as the primary manager: KeyError → 404 for an
        unknown id, ValueError → 409 for a forbidden transition."""
        status = self._status_of(task_id)
        if status == "unknown":
            raise KeyError(f"no such task: {task_id}")
        if status not in allowed:
            raise ValueError(f"cannot do that to a task that is {status}")
        return status

    def _status_of(self, task_id: str) -> str:
        with SessionLocal() as db:
            task = db.get(AgentTask, task_id)
            return task.status if task and task.kind == "task" else "unknown"

    def _set_status(self, task_id: str, status: str) -> None:
        with SessionLocal() as db:
            task = db.get(AgentTask, task_id)
            if task:
                task.status = status
                db.commit()

    async def _stop_runner(self, task_id: str) -> None:
        """Cancel this task's runner, if one is live."""
        runner = self._runners.pop(task_id, None)
        if runner and not runner.done():
            runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runner

    async def _pump(self) -> None:
        """Start queued tasks while the pool has room (oldest first)."""
        if self._closing:
            return                       # no new work during shutdown
        # Drop finished runners from the pool bookkeeping.
        self._runners = {tid: r for tid, r in self._runners.items()
                         if not r.done()}
        room = MAX_CONCURRENT - len(self._runners)
        if room <= 0:
            return
        with SessionLocal() as db:
            queued = (db.query(AgentTask)
                      .filter_by(status="queued", kind="task")
                      .order_by(AgentTask.created_at).limit(room).all())
            for task in queued:
                task.status = "running"
            db.commit()
        for task in queued:
            self._runners[task.id] = asyncio.create_task(
                self._run(task.id), name=f"task:{task.id[:8]}")
            bus.publish("tasks", "started", task_id=task.id)

    async def _run(self, task_id: str) -> None:
        """The loop for ONE discrete task — the primary loop's shape,
        without battery pacing (this is foreground work the user wants
        soon) and with a tighter step budget."""
        steps = 0
        last_call: Optional[str] = None
        repeats = 0
        try:
            while True:
                # Still supposed to be running? (pause/cancel are written
                # by other coroutines; this is the check.)
                if self._status_of(task_id) != "running":
                    return
                steps += 1
                if steps > MAX_STEPS:
                    # Budget spent: don't just stop — make it CONCLUDE.
                    # One tool-free round turns a half-finished task into
                    # a reported one (or an honest BLOCKED).
                    journal(task_id, "status",
                            f"Step budget ({MAX_STEPS}) reached — "
                            "summarizing what I have.")
                    await self._force_finish(task_id, "budget")
                    return

                # One think→act cycle at TIER 2 (the whole difference).
                try:
                    outcome = await run_step(task_id, tier=Tier.FOREGROUND_TASK)
                except asyncio.CancelledError:
                    raise
                except PreemptedError:
                    # Live chat took the slot; checkpointing is durable,
                    # so just note it and retry.
                    journal(task_id, "status", "Yielded to live chat; retrying.")
                    continue
                except ModelUnloadingError:
                    # The model is being unloaded: retrying would hold
                    # the unload hostage. Pause — resumable after the
                    # next Load (the checkpoint journal survives).
                    self._set_status(task_id, "paused")
                    journal(task_id, "status",
                            "Paused — the model was unloaded. Resume "
                            "after loading a model.")
                    bus.publish("tasks", "paused", task_id=task_id,
                                reason="unload")
                    return
                except Exception as error:
                    logger.exception("discrete task step failed")
                    journal(task_id, "error", f"Step failed: {error}")
                    await asyncio.sleep(5.0)
                    continue

                if outcome.kind == "done":
                    self._finish(task_id, outcome.text)
                    return
                if outcome.kind in ("blocked", "ask"):
                    self._set_status(task_id, "blocked")
                    journal(task_id, "approval_request", outcome.text)
                    # The question belongs in the conversation too — that
                    # is where the user is watching the task from.
                    self._persist_chat_message(
                        task_id,
                        "The task needs your input: " + outcome.text
                        + "\n(Answer from the Agent tab to continue.)")
                    bus.publish("tasks", "blocked", task_id=task_id,
                                question=outcome.text)
                    return

                # Stuck detection, from the outcome's call signature.
                if outcome.call_sig is not None:
                    repeats = repeats + 1 if outcome.call_sig == last_call else 0
                    last_call = outcome.call_sig
                    if repeats >= REPEAT_PAUSE:
                        # Repeating one call means it already has what it
                        # needs and doesn't know to stop. Take the tools
                        # away and ask for the answer — measured: agent
                        # tasks that used to pause here now finish.
                        journal(task_id, "status",
                                "Repeating a tool call — answering from "
                                "what I have.")
                        await self._force_finish(task_id, "stuck")
                        return
                # A short breath between steps (foreground: keep it snappy).
                await asyncio.sleep(0.5)
        finally:
            # Whatever ended this run: free the pool seat and serve the
            # queue. (create/pause/cancel also pump; this covers done,
            # blocked, budget, stuck, and crashes.)
            self._runners.pop(task_id, None)
            asyncio.get_running_loop().create_task(self._pump())

    async def _force_finish(self, task_id: str, reason: str) -> None:
        """The convergence handshake: one round with NO tools, demanding
        a conclusion. A model that can't reach for a tool has to answer.
        If even that fails, THEN pause — a genuinely stuck task is worth
        a human's eyes, but only after we've asked it to just report."""
        try:
            outcome = await run_step(task_id, tier=Tier.FOREGROUND_TASK,
                                     force_answer=True)
        except Exception as error:
            logger.exception("forced finish failed")
            outcome = None
        if outcome is not None and outcome.kind == "done" and outcome.text:
            self._finish(task_id, outcome.text)
            return
        self._set_status(task_id, "paused")
        journal(task_id, "error",
                "Could not conclude — paused for review.")
        bus.publish("tasks", "paused", task_id=task_id, reason=reason)

    def _finish(self, task_id: str, report: str) -> None:
        """Mark done and store the final report."""
        with SessionLocal() as db:
            task = db.get(AgentTask, task_id)
            task.status = "done"
            task.result = report
            db.commit()
        journal(task_id, "status", "Task complete.")
        # The result lands IN the conversation that asked for it — the
        # organizer tab is for organizing, not for delivering.
        self._persist_chat_message(task_id, "Task complete.\n\n" + report)
        bus.publish("tasks", "done", task_id=task_id, report=report[:500])

    def _persist_chat_message(self, task_id: str, content: str) -> None:
        """Write a task outcome into the conversation that started it.

        Synchronous on purpose (the chat route's own rule): a WAL commit
        is ~a millisecond, and these are called from paths that must not
        be interruptible mid-write. Tasks started outside chat (none
        today) simply skip this.
        """
        try:
            with SessionLocal() as db:
                task = db.get(AgentTask, task_id)
                if not task or not task.session_id:
                    return
                db.add(Message(session_id=task.session_id, role="assistant",
                               content=content))
                session = db.get(ChatSession, task.session_id)
                if session:              # outcomes are activity (ordering)
                    session.updated_at = utcnow()
                db.commit()
        except Exception:
            logger.exception("could not write the task outcome to chat")

    async def pause_all(self) -> int:
        """Pause every running task (cancel-and-unload's graceful half:
        checkpoints are durable, so paused tasks resume after the next
        Load). Returns how many were paused."""
        count = 0
        for task_id in list(self._runners):
            try:
                if self._status_of(task_id) == "running":
                    journal(task_id, "status", "Model unloading — pausing.")
                    await self.pause(task_id)
                    count += 1
            except (KeyError, ValueError):
                continue               # raced into a terminal state — fine
        return count

    async def cancel_session(self, session_id: str) -> int:
        """Cancel this conversation's non-terminal tasks (4.1: deleting a
        conversation must not orphan its work). Awaited — the caller only
        proceeds with the delete once every runner has actually stopped.
        Paused tasks are left alone: they are dormant, resumable from the
        Agent tab, and their rows are retained by policy."""
        with SessionLocal() as db:
            rows = (db.query(AgentTask)
                    .filter(AgentTask.kind == "task",
                            AgentTask.session_id == session_id,
                            AgentTask.status.in_(("running", "queued",
                                                  "blocked")))
                    .all())
            ids = [t.id for t in rows]
        for task_id in ids:
            try:
                await self.cancel(task_id)
            except (KeyError, ValueError):
                continue               # raced into a terminal state — fine
        return len(ids)

    # -------------------------------------------------------------- queries
    def overview(self) -> dict:
        """Everything the Agent (organizer) tab needs in one call."""
        with SessionLocal() as db:
            tasks = (db.query(AgentTask).filter_by(kind="task")
                     .order_by(AgentTask.created_at.desc()).limit(100).all())
            # Which originating conversations still EXIST: a task whose
            # chat was deleted keeps its row (retained, per the
            # tombstone policy) but its "Open chat" control must say so
            # instead of opening a blank ghost (bug 4.3).
            wanted = {t.session_id for t in tasks if t.session_id}
            existing = ({row[0] for row in
                         db.query(ChatSession.id)
                           .filter(ChatSession.id.in_(wanted))}
                        if wanted else set())
            return {
                "running": [tid for tid, r in self._runners.items() if not r.done()],
                "tasks": [
                    {
                        "id": t.id, "goal": t.goal, "status": t.status,
                        "notes": t.notes, "result": t.result,
                        "session_id": t.session_id or "",
                        "session_exists": t.session_id in existing,
                        "created_at": t.created_at.isoformat(),
                    }
                    for t in tasks
                ],
            }


# The app-wide instance (constructed here; started by app.py's lifespan).
discrete = DiscreteTaskManager()
