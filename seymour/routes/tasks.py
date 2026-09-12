"""Discrete agent tasks' HTTP surface: chat-started jobs, organized.

The Agent tab renders from here: every discrete (Tier 2) job past and
present, its journal, and the lifecycle controls. The primary agent's
continuous seat lives at /api/agent; this is the other kind of work.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from seymour import runtime
from seymour.agent.discrete import discrete
from seymour.db import AgentStep, SessionLocal

router = APIRouter(prefix="/api/tasks")


class NewTask(BaseModel):
    """A new discrete job: the goal, and (optionally) the chat it came from."""

    goal: str
    session_id: str = ""


class TaskResponse(BaseModel):
    """The user's answer to an ask_user question."""

    answer: str


async def _lifecycle(action) -> dict:
    """The same error mapping as the primary agent's routes: KeyError →
    404 (unknown id), ValueError → 409 (forbidden transition)."""
    try:
        return {"status": await action}
    except KeyError as error:
        raise HTTPException(404, str(error))
    except ValueError as error:
        raise HTTPException(409, str(error))


@router.get("")
async def overview():
    """Every discrete task, newest first, plus which are live right now."""
    return discrete.overview()


@router.post("")
async def create(body: NewTask):
    """Start a discrete agent job (runs now if the pool has room)."""
    if not body.goal.strip():
        raise HTTPException(422, "goal must not be empty")
    if runtime.scheduler is None:
        raise HTTPException(
            503, "No model is loaded yet — load one in the Models tab.")
    return await discrete.create(body.goal, body.session_id)


@router.get("/{task_id}/journal")
async def journal(task_id: str):
    """The task's full visible work log (live updates ride /api/events)."""
    with SessionLocal() as db:
        steps = (db.query(AgentStep).filter_by(task_id=task_id)
                 .order_by(AgentStep.id.desc()).limit(200).all())
    return [
        {"id": s.id, "kind": s.kind, "content": s.content,
         "at": s.created_at.isoformat()}
        for s in reversed(steps)
    ]


@router.post("/{task_id}/pause")
async def pause(task_id: str):
    """Stop the loop; keep every checkpoint."""
    return await _lifecycle(discrete.pause(task_id))


@router.post("/{task_id}/resume")
async def resume(task_id: str):
    """Continue from the checkpoint (queues if the pool is full)."""
    return await _lifecycle(discrete.resume(task_id))


@router.post("/{task_id}/cancel")
async def cancel(task_id: str):
    """Abandon the job for good; the journal stays readable."""
    return await _lifecycle(discrete.cancel(task_id))


@router.post("/{task_id}/respond")
async def respond(task_id: str, body: TaskResponse):
    """Answer the task's ask_user question and let it continue."""
    return await _lifecycle(discrete.respond(task_id, body.answer))
