"""Memory's HTTP surface: the reviewable store the user owns."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from seymour.memory.store import (
    add_memory,
    delete_memory,
    list_memories,
    retrieve,
    set_pinned,
)

router = APIRouter(prefix="/api/memories")


class NewMemory(BaseModel):
    """A fact the user wants Seymour to keep."""

    content: str
    kind: str = "fact"


class PinBody(BaseModel):
    """Pin state for one memory."""

    pinned: bool


@router.get("")
async def all_memories():
    """Every memory, pinned first — full transparency into the store."""
    return list_memories()


@router.post("")
async def create(body: NewMemory):
    """Add a fact by hand (dedup applies just like automatic saves).
    The store validates the category (unknown kinds degrade to 'fact')."""
    if not body.content.strip():
        raise HTTPException(422, "content must not be empty")
    return await add_memory(body.content, kind=body.kind, source="user")


@router.post("/{memory_id}/pin")
async def pin(memory_id: int, body: PinBody):
    """Pin/unpin: pinned identity/contact facts ride in every chat;
    other pins win retrieval ties."""
    if not set_pinned(memory_id, body.pinned):
        raise HTTPException(404, "no such memory")
    return {"id": memory_id, "pinned": body.pinned}


@router.get("/search")
async def search(q: str):
    """The same hybrid retrieval chat uses — so the user can SEE what
    would be injected for any given query. Transparency builds trust."""
    return await retrieve(q)


@router.delete("/{memory_id}")
async def forget(memory_id: int):
    """The user's right to make Seymour forget."""
    if not delete_memory(memory_id):
        raise HTTPException(404, "no such memory")
    return {"deleted": memory_id}
