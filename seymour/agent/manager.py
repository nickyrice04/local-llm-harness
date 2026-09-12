"""The agent manager: task lifecycle, and the loop that runs it.

One primary task runs at a time — that is the product concept: the primary
agent is Seymour dedicated to ONE long-running job, not a task farm. More
tasks queue behind it (the scheduler's Tier 2 exists for parallel
foreground jobs like research).

The lifecycle state machine (mirrored in db.AgentTask.status):

    queued → running → done | failed | cancelled
                ↕
             paused    (user asked; or the step budget ran out)
                ↕
             blocked   (waiting for the user to answer ask_user)

Every transition is published on the event bus — the avatar renders these
transitions and nothing else (the Papert rule: the face reflects the real
state machine, never a timer).
"""

import asyncio
import contextlib
import json
import logging
import uuid
from typing import Optional

from seymour import runtime
from seymour.agent import power
from seymour.agent.loop import journal, run_step
from seymour.config import settings
from seymour.db import AgentStep, AgentTask, SessionLocal
from seymour.engine.adapter import GenerationRequest
from seymour.events import bus
from seymour.prompts import load
from seymour.scheduler.tiers import ModelUnloadingError, PreemptedError, Tier

logger = logging.getLogger(__name__)

# ---- Bounded counters (every "might never stop" path gets one) ------------ #
# Steps per run segment: when it runs out the task PAUSES (honestly, with a
# journal entry) rather than burning tokens forever. Resume continues it.
MAX_STEPS_PER_RUN = 100
# Consecutive tool-free "thoughts" before we nudge the model to act.
# Resets whenever a step contains a tool call.
MAX_CONSECUTIVE_THOUGHTS = 3
# Identical consecutive tool calls before a warning is injected (6 = pause).
# Resets whenever a different call appears.
REPEAT_WARN = 3
REPEAT_PAUSE = 6


class AgentManager:
    """Owns the (single) runner task and every lifecycle transition."""

    def __init__(self) -> None:
        # The asyncio task executing _run(), when a task is active.
        self._runner: Optional[asyncio.Task] = None
        # The id of the task the runner is working on.
        self.current_task_id: Optional[str] = None

    # ------------------------------------------------------------- lifecycle
    async def startup(self) -> None:
        """Called once at boot: resume whatever the last session left off.

        A task that was 'running' when the app quit is the checkpoint
        contract in action — its notes and journal are all it needs.
        Historical bugs could leave MORE than one row 'running'; only the
        newest resumes, the rest are honestly re-queued, never stranded.
        """
        with SessionLocal() as db:
            running = (db.query(AgentTask)
                       .filter_by(status="running", kind="primary")
                       .order_by(AgentTask.created_at.desc()).all())
            for stale in running[1:]:
                stale.status = "queued"
            db.commit()
        if running:
            journal(running[0].id, "status", "Seymour restarted — resuming from checkpoint.")
            self._start_runner(running[0].id)
        else:
            await self._start_next_queued()

    async def shutdown(self) -> None:
        """Called at app exit: stop the loop cleanly. The task row stays
        'running' on purpose — that is what startup() resumes."""
        if self._runner and not self._runner.done():
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner

    def _start_runner(self, task_id: str) -> None:
        """Spin up the loop for one task (exactly one runner at a time)."""
        self.current_task_id = task_id
        self._runner = asyncio.create_task(self._run(task_id), name=f"agent:{task_id[:8]}")
        bus.publish("agent", "task_started", task_id=task_id)

    async def _start_next_queued(self) -> None:
        """Promote the oldest queued task, if the seat is free.

        The seat is free when no runner is live — OR when the live runner
        is the very task calling us (a finishing task promoting its
        successor: an asyncio.Task is not .done() while its own coroutine
        is still executing, so without the current_task() exception a
        finishing task would see itself and never hand over the seat).
        """
        if (self._runner and not self._runner.done()
                and self._runner is not asyncio.current_task()):
            return                    # a DIFFERENT task is already running
        with SessionLocal() as db:
            next_task = (db.query(AgentTask)
                         .filter_by(status="queued", kind="primary")
                         .order_by(AgentTask.created_at).first())
            if next_task is None:
                self.current_task_id = None    # seat is truly empty now
                bus.publish("agent", "idle")   # avatar: tea time
                return
            next_task.status = "running"
            db.commit()
        self._start_runner(next_task.id)

    # ----------------------------------------------------------- public api
    async def create_task(self, goal: str) -> dict:
        """Accept a new job. Runs now if the seat is free, queues otherwise."""
        task = AgentTask(id=str(uuid.uuid4()), goal=goal.strip(), kind="primary")
        with SessionLocal() as db:
            db.add(task)
            db.commit()
        bus.publish("agent", "task_created", task_id=task.id, goal=task.goal)
        await self._start_next_queued()
        return {"id": task.id, "status": self._status_of(task.id)}

    def _require(self, task_id: str, allowed: tuple[str, ...]) -> str:
        """Validate a lifecycle request BEFORE mutating anything.

        Raises KeyError for an id that doesn't exist (route → 404) and
        ValueError for a transition the state machine forbids (route →
        409). Without this, a stale UI or a typo'd curl silently wrote
        journal rows for phantom tasks and revived finished ones.
        """
        status = self._status_of(task_id)
        if status == "unknown":
            raise KeyError(f"no such task: {task_id}")
        if status not in allowed:
            raise ValueError(f"cannot do that to a task that is {status}")
        return status

    async def pause(self, task_id: str) -> str:
        """User pressed pause: stop the loop, keep everything."""
        self._require(task_id, allowed=("running", "queued", "blocked", "paused"))
        self._set_status(task_id, "paused")
        await self._stop_runner_if(task_id)
        journal(task_id, "status", "Paused by user.")
        bus.publish("agent", "task_paused", task_id=task_id)
        return "paused"

    async def pause_all(self) -> int:
        """Pause whatever is running on the seat (cancel-and-unload's
        graceful half — the checkpoint makes it resumable)."""
        count = 0
        with SessionLocal() as db:
            running = (db.query(AgentTask)
                       .filter(AgentTask.kind != "task",
                               AgentTask.status == "running").all())
            ids = [t.id for t in running]
        for task_id in ids:
            try:
                journal(task_id, "status", "Model unloading — pausing.")
                await self.pause(task_id)
                count += 1
            except (KeyError, ValueError):
                continue
        return count

    async def resume(self, task_id: str) -> str:
        """Continue a paused or blocked task from its checkpoint.

        If the seat is occupied by ANOTHER task, this task goes back to
        'queued' — it will start the moment the seat frees, promoted by
        the finishing task. Marking it 'running' with no runner (the old
        behaviour) stranded it forever.
        """
        status = self._require(
            task_id, allowed=("paused", "blocked", "queued", "running"))
        if status == "running" and self.current_task_id == task_id:
            return "running"          # already running: idempotent no-op
        seat_taken = (self._runner and not self._runner.done()
                      and self.current_task_id != task_id)
        if seat_taken:
            self._set_status(task_id, "queued")
            journal(task_id, "status",
                    "Resumed — waiting for the current task to finish.")
            bus.publish("agent", "task_resumed", task_id=task_id, queued=True)
            return "queued"
        self._set_status(task_id, "running")
        journal(task_id, "status", "Resumed.")
        self._start_runner(task_id)
        bus.publish("agent", "task_resumed", task_id=task_id)
        return "running"

    async def cancel(self, task_id: str) -> str:
        """Abandon a task for good (its journal remains readable)."""
        self._require(task_id, allowed=("running", "queued", "paused",
                                        "blocked", "cancelled"))
        self._set_status(task_id, "cancelled")
        await self._stop_runner_if(task_id)
        journal(task_id, "status", "Cancelled by user.")
        bus.publish("agent", "task_cancelled", task_id=task_id)
        await self._start_next_queued()
        return "cancelled"

    async def respond(self, task_id: str, answer: str) -> str:
        """The user answered an ask_user question: unblock and continue."""
        # Only a task actually waiting on a question can take an answer.
        self._require(task_id, allowed=("blocked",))
        journal(task_id, "approval_response", answer)
        return await self.resume(task_id)

    # ------------------------------------------------------------- internals
    def _set_status(self, task_id: str, status: str) -> None:
        """One place writes task status, so transitions stay auditable."""
        with SessionLocal() as db:
            task = db.get(AgentTask, task_id)
            if task:
                task.status = status
                db.commit()

    def _status_of(self, task_id: str) -> str:
        with SessionLocal() as db:
            task = db.get(AgentTask, task_id)
            return task.status if task else "unknown"

    async def _stop_runner_if(self, task_id: str) -> None:
        """Cancel the runner if it is working on this task."""
        if self.current_task_id == task_id and self._runner and not self._runner.done():
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner

    async def _run(self, task_id: str) -> None:
        """THE loop. Small on purpose — each concern is one guarded block."""
        steps = 0                     # counts toward MAX_STEPS_PER_RUN
        consecutive_thoughts = 0      # resets on any tool call
        last_call: Optional[str] = None   # for repeat detection
        repeats = 0                   # consecutive identical tool calls

        while True:
            # 1. Am I still supposed to be running? (pause/cancel/block are
            #    written to the DB by other coroutines; this is the check.)
            if self._status_of(task_id) != "running":
                return

            # 2. Step budget — the loop's own bounded counter.
            steps += 1
            if steps > MAX_STEPS_PER_RUN:
                self._set_status(task_id, "paused")
                journal(task_id, "status",
                        f"Step budget ({MAX_STEPS_PER_RUN}) reached — pausing. "
                        "Press resume to continue.")
                bus.publish("agent", "task_paused", task_id=task_id, reason="budget")
                return

            # 3. Battery politeness: on battery the pause between steps
            #    stretches, and the UI is told exactly that.
            pause = settings.agent_step_pause
            if power.on_battery():
                pause *= settings.agent_battery_pause_factor
                bus.publish("agent", "throttled", task_id=task_id,
                            battery=power.battery_percent())

            # 4. One think→act cycle, with the preemption contract applied.
            try:
                outcome = await run_step(task_id)
            except asyncio.CancelledError:
                raise                 # shutdown/pause — let it propagate
            except PreemptedError:
                # The scheduler took our slot for a human. Checkpointing is
                # already durable (journal + notes), so just note it and
                # retry — the floor promotion guarantees we get back in.
                journal(task_id, "status", "Yielded to a foreground request; retrying.")
                bus.publish("agent", "preempted", task_id=task_id)
                continue
            except ModelUnloadingError:
                # The model is being unloaded: pause rather than retry
                # (retrying would hold the unload hostage). The task
                # resumes from its checkpoint after the next Load.
                self._set_status(task_id, "paused")
                journal(task_id, "status",
                        "Paused — the model was unloaded. Resume after "
                        "loading a model.")
                bus.publish("agent", "task_paused", task_id=task_id)
                return
            except Exception as error:
                # A step blew up (network, engine hiccup…). Journal it and
                # back off — one bad step must not kill a long task.
                logger.exception("agent step failed")
                journal(task_id, "error", f"Step failed: {error}")
                await asyncio.sleep(max(pause, 10.0))
                continue

            # 5. Act on the outcome.
            if outcome.kind == "done":
                if await self._verify(task_id, outcome.text):
                    self._finish(task_id, outcome.text)
                    await self._start_next_queued()
                    return
                # Verification failed: the finding is in the journal; keep
                # working (counts against the step budget, so it is bounded).
                continue

            if outcome.kind in ("blocked", "ask"):
                # Human-in-the-loop: park the task and surface the question.
                self._set_status(task_id, "blocked")
                journal(task_id, "approval_request", outcome.text)
                bus.publish("agent", "task_blocked", task_id=task_id,
                            question=outcome.text)
                return

            # outcome.kind == "continue": update the stuck-detection state.
            # The outcome itself says whether this step called a tool
            # (call_sig is the canonical tool+args signature) — asking the
            # journal doesn't work, because by now the newest entry is the
            # tool RESULT, not the call.
            if outcome.call_sig is not None:
                consecutive_thoughts = 0
                # Identical call repeated? Warn, then pause (never spin).
                if outcome.call_sig == last_call:
                    repeats += 1
                else:
                    repeats, last_call = 0, outcome.call_sig
                if repeats == REPEAT_WARN:
                    journal(task_id, "status",
                            "You have made the same call several times with the "
                            "same result. Change approach, or finish with DONE:.")
                if repeats >= REPEAT_PAUSE:
                    self._set_status(task_id, "paused")
                    journal(task_id, "error",
                            "Stuck repeating one tool call — pausing for review.")
                    bus.publish("agent", "task_paused", task_id=task_id, reason="stuck")
                    return
            else:
                consecutive_thoughts += 1
                if consecutive_thoughts >= MAX_CONSECUTIVE_THOUGHTS:
                    journal(task_id, "status",
                            "You have been thinking without acting. Make a tool "
                            "call now, or finish with DONE:.")
                    consecutive_thoughts = 0

            # 6. Breathe. Politeness to the scheduler and the battery both.
            await asyncio.sleep(pause)

    def _finish(self, task_id: str, report: str) -> None:
        """Mark done and store the final report."""
        with SessionLocal() as db:
            task = db.get(AgentTask, task_id)
            task.status = "done"
            task.result = report
            db.commit()
        journal(task_id, "status", "Task complete.")
        bus.publish("agent", "task_done", task_id=task_id, report=report[:500])

    async def _verify(self, task_id: str, report: str) -> bool:
        """Fresh-context completion check. FAILS OPEN: any error passes.

        A model checking its own work rationalizes; one that didn't do the
        work reads it cold (the reference implementation's insight). One
        extra Tier 3 request per completed task is cheap insurance.
        """
        with SessionLocal() as db:
            task = db.get(AgentTask, task_id)
            # A condensed journal: kinds + first lines only.
            condensed = "\n".join(
                f"[{s.kind}] {s.content[:200]}" for s in task.steps[-30:]
            )
        request = GenerationRequest(
            messages=[{"role": "user", "content": load(
                "verifier", goal=task.goal, report=report, journal=condensed,
            )}],
            max_tokens=256,
            temperature=0.0,
            # NOTE: no cache_key — fresh context is the whole point.
        )
        try:
            verdict = await runtime.scheduler.complete(
                Tier.BACKGROUND_AGENT, request, label=f"verify:{task_id[:8]}"
            )
        except Exception:
            return True               # fail open — never block a completion
        if "VERIFICATION: FAIL" in verdict:
            reason = verdict.split("VERIFICATION: FAIL:", 1)[-1].strip()[:300]
            journal(task_id, "status", f"Completion check disagreed: {reason}")
            return False
        return True

    # -------------------------------------------------------------- queries
    def overview(self) -> dict:
        """Everything the Agent panel needs in one call."""
        with SessionLocal() as db:
            tasks = (db.query(AgentTask).filter_by(kind="primary")
                     .order_by(AgentTask.created_at.desc()).limit(50).all())
            return {
                "current_task_id": self.current_task_id,
                "on_battery": power.on_battery(),
                "tasks": [
                    {
                        "id": t.id, "goal": t.goal, "status": t.status,
                        "notes": t.notes, "result": t.result,
                        "created_at": t.created_at.isoformat(),
                    }
                    for t in tasks
                ],
            }

    def journal_of(self, task_id: str, limit: int = 200) -> list[dict]:
        """A task's journal, oldest first, for the UI's live log."""
        with SessionLocal() as db:
            steps = (db.query(AgentStep).filter_by(task_id=task_id)
                     .order_by(AgentStep.id.desc()).limit(limit).all())
        return [
            {"id": s.id, "kind": s.kind, "content": s.content,
             "at": s.created_at.isoformat()}
            for s in reversed(steps)
        ]
