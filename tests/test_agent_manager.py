"""Regression proofs for the agent-manager bugs the adversarial review
confirmed (2026-08): the queue that never advanced, the resume that
stranded tasks, and lifecycle endpoints that accepted phantom ids.

The loop's model call (run_step) is stubbed per test — these are tests of
the MANAGER's state machine, and the real step would need an engine.
"""

import asyncio
import contextlib

import pytest

import seymour.agent.manager as manager_mod
from seymour.agent.loop import StepOutcome
from seymour.agent.manager import AgentManager
from seymour.db import AgentStep, AgentTask, SessionLocal, init_db


@pytest.fixture(autouse=True)
def clean_tasks():
    """Every test starts with an empty task table (shared test database)."""
    init_db()
    with SessionLocal() as db:
        db.query(AgentStep).delete()
        db.query(AgentTask).delete()
        db.commit()
    yield


@pytest.fixture(autouse=True)
def fast_loop(monkeypatch):
    """No sleeping between steps, and verification always passes —
    the manager's own logic is what's under test here."""
    monkeypatch.setattr(manager_mod.settings, "agent_step_pause", 0.001)

    async def verify_ok(self, task_id, report):
        return True
    monkeypatch.setattr(AgentManager, "_verify", verify_ok)


async def wait_for_status(task_id: str, wanted: str, timeout: float = 3.0) -> str:
    """Poll the DB until the task reaches `wanted` (or time runs out)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        with SessionLocal() as db:
            task = db.get(AgentTask, task_id)
            if task and task.status == wanted:
                return task.status
        await asyncio.sleep(0.01)
    with SessionLocal() as db:
        task = db.get(AgentTask, task_id)
        return task.status if task else "unknown"


# --------------------------------------------------------------------------- #
#  Bug 1 — a finishing task must hand the seat to the next queued task        #
# --------------------------------------------------------------------------- #

async def test_queued_task_starts_after_current_finishes(monkeypatch):
    async def instant_done(task_id):
        return StepOutcome("done", "finished")
    monkeypatch.setattr(manager_mod, "run_step", instant_done)

    manager = AgentManager()
    a = await manager.create_task("task A")
    b = await manager.create_task("task B")      # queues behind A
    # Before the fix, A finished and B sat 'queued' forever — the
    # promotion guard saw A's own still-running asyncio task and bailed.
    assert await wait_for_status(a["id"], "done") == "done"
    assert await wait_for_status(b["id"], "done") == "done"
    await manager.shutdown()


async def test_seat_clears_when_queue_empties(monkeypatch):
    async def instant_done(task_id):
        return StepOutcome("done", "finished")
    monkeypatch.setattr(manager_mod, "run_step", instant_done)

    manager = AgentManager()
    a = await manager.create_task("only task")
    assert await wait_for_status(a["id"], "done") == "done"
    await asyncio.sleep(0.05)                    # let the runner unwind
    # The finished task must not be reported as current forever.
    assert manager.overview()["current_task_id"] is None
    await manager.shutdown()


# --------------------------------------------------------------------------- #
#  Bug 2 — resume while the seat is taken queues instead of stranding         #
# --------------------------------------------------------------------------- #

async def test_resume_while_seat_taken_queues_the_task(monkeypatch):
    release = asyncio.Event()                    # holds task A mid-step

    async def stepper(task_id):
        with SessionLocal() as db:               # which task is this step for?
            goal = db.get(AgentTask, task_id).goal
        if goal == "task A":
            await release.wait()                 # A occupies the seat…
        return StepOutcome("done", "finished")   # …until released

    monkeypatch.setattr(manager_mod, "run_step", stepper)
    manager = AgentManager()
    a = await manager.create_task("task A")      # seizes the seat
    b = await manager.create_task("task B")      # queues
    await asyncio.sleep(0.05)
    # Pause B (legal from 'queued'), then resume it while A still runs.
    await manager.pause(b["id"])
    status = await manager.resume(b["id"])
    # Before the fix: B was marked 'running' with NO runner — stranded
    # forever. Now it goes back to 'queued' and starts when A finishes.
    assert status == "queued"
    release.set()
    assert await wait_for_status(a["id"], "done") == "done"
    assert await wait_for_status(b["id"], "done") == "done"
    await manager.shutdown()


# --------------------------------------------------------------------------- #
#  Bug 3 — lifecycle calls validate existence and state                       #
# --------------------------------------------------------------------------- #

async def test_lifecycle_rejects_unknown_ids():
    manager = AgentManager()
    for method in (manager.pause, manager.resume, manager.cancel):
        with pytest.raises(KeyError):
            await method("no-such-task")
    with pytest.raises(KeyError):
        await manager.respond("no-such-task", "an answer")
    # No orphan journal rows were written for the phantom id.
    with SessionLocal() as db:
        assert db.query(AgentStep).count() == 0


async def test_finished_tasks_cannot_be_revived(monkeypatch):
    async def instant_done(task_id):
        return StepOutcome("done", "finished")
    monkeypatch.setattr(manager_mod, "run_step", instant_done)

    manager = AgentManager()
    a = await manager.create_task("one and done")
    assert await wait_for_status(a["id"], "done") == "done"
    # Resuming (or answering) a DONE task is a state-machine violation.
    with pytest.raises(ValueError):
        await manager.resume(a["id"])
    with pytest.raises(ValueError):
        await manager.respond(a["id"], "hello?")
    assert await wait_for_status(a["id"], "done") == "done"
    await manager.shutdown()


async def test_startup_requeues_extra_running_rows(monkeypatch):
    stepped = asyncio.Event()

    async def one_step(task_id):
        stepped.set()
        return StepOutcome("done", "finished")
    monkeypatch.setattr(manager_mod, "run_step", one_step)

    # Simulate the corrupt state older bugs could leave: two rows 'running'.
    import uuid
    ids = []
    with SessionLocal() as db:
        for name in ("older", "newer"):
            task = AgentTask(id=str(uuid.uuid4()), goal=name, status="running")
            db.add(task)
            ids.append(task.id)
        db.commit()
    manager = AgentManager()
    await manager.startup()
    # Both must finish eventually: one resumes now, the other re-queues
    # and is promoted when the first completes (never stranded 'running').
    assert await wait_for_status(ids[0], "done") == "done"
    assert await wait_for_status(ids[1], "done") == "done"
    await manager.shutdown()
