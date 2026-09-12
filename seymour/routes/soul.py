"""The soul editor's HTTP surface: read and rewrite Seymour's character."""

from fastapi import APIRouter
from pydantic import BaseModel

from seymour.persona.soul import get_soul, set_soul

router = APIRouter(prefix="/api/soul")


class SoulBody(BaseModel):
    """The full replacement soul text."""

    text: str


@router.get("")
async def read_soul():
    """The current soul, verbatim (it's the user's file)."""
    return {"text": get_soul()}


@router.put("")
async def write_soul(body: SoulBody):
    """Replace the soul. Takes effect on the next model request — the
    system prompt is rebuilt from the file every time."""
    set_soul(body.text)
    return {"saved": True}
