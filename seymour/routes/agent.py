"""The primary agent's HTTP surface: give it work, watch it, steer it."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from seymour import runtime

router = APIRouter(prefix="/api/agent")


class NewTask(BaseModel):
    """The one field a new task needs: what to do."""

    goal: str


class TaskResponse(BaseModel):
    """The user's answer to an ask_user question."""

    answer: str


@router.get("")
async def overview():
    """Everything the Agent panel renders: tasks, statuses, battery."""
    return runtime.agent.overview()


@router.post("/tasks")
async def create_task(body: NewTask):
    """Hand the primary agent a new long-running job."""
    if not body.goal.strip():
        raise HTTPException(422, "goal must not be empty")
    # Setup mode: the agent can't think without a model.
    if runtime.scheduler is None:
        raise HTTPException(
            503, "No model is loaded yet — download or activate one in the "
                 "Models tab.")
    return await runtime.agent.create_task(body.goal)


@router.get("/tasks/{task_id}/journal")
async def journal(task_id: str):
    """The task's full visible work log (the UI also gets live updates
    over /api/events; this endpoint is the catch-up on open)."""
    return runtime.agent.journal_of(task_id)


async def _lifecycle(action) -> dict:
    """Shared error mapping for every lifecycle endpoint: the manager
    raises KeyError for an unknown id (→ 404) and ValueError for a
    transition its state machine forbids (→ 409). Before this mapping,
    a stale UI could journal onto phantom tasks and revive finished ones."""
    try:
        return {"status": await action}
    except KeyError as error:
        raise HTTPException(404, str(error))
    except ValueError as error:
        raise HTTPException(409, str(error))


@router.post("/tasks/{task_id}/pause")
async def pause(task_id: str):
    """Stop the loop; keep every checkpoint."""
    return await _lifecycle(runtime.agent.pause(task_id))


@router.post("/tasks/{task_id}/resume")
async def resume(task_id: str):
    """Continue from the checkpoint (also unblocks a blocked task).
    Returns 'running', or 'queued' if another task holds the seat."""
    return await _lifecycle(runtime.agent.resume(task_id))


@router.post("/tasks/{task_id}/cancel")
async def cancel(task_id: str):
    """Abandon the task for good; the journal stays readable."""
    return await _lifecycle(runtime.agent.cancel(task_id))


@router.post("/tasks/{task_id}/respond")
async def respond(task_id: str, body: TaskResponse):
    """Answer the agent's ask_user question and let it continue."""
    return await _lifecycle(runtime.agent.respond(task_id, body.answer))
