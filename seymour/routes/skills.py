"""Skills' HTTP surface: list, read, enable/disable, create."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from seymour import skills

router = APIRouter(prefix="/api/skills")


@router.get("")
async def list_skills():
    """Every skill found, with its source and whether it is in the index."""
    return {"skills": [s.to_dict() for s in sorted(skills.all_skills(), key=lambda s: s.name)],
            "index": skills.index_text()}


@router.get("/{name}")
async def read_skill(name: str):
    """A skill's full text (what use_skill hands the model)."""
    skill = skills.get(name)
    if skill is None:
        raise HTTPException(404, f"no skill named {name}")
    return {**skill.to_dict(), "body": skill.body}


class EnabledBody(BaseModel):
    enabled: bool


@router.post("/{name}/enabled")
async def set_enabled(name: str, body: EnabledBody):
    try:
        return skills.set_enabled(name, body.enabled).to_dict()
    except KeyError:
        raise HTTPException(404, f"no skill named {name}")


class CreateBody(BaseModel):
    name: str
    description: str = ""
    body: str = ""


@router.post("")
async def create_skill(body: CreateBody):
    """Write a new skill under ~/.seymour/skills/<name>/SKILL.md."""
    try:
        return skills.create(body.name, body.description, body.body or
                             "# Steps\n\n1. …\n").to_dict()
    except ValueError as error:
        raise HTTPException(422, str(error))
    except FileExistsError as error:
        raise HTTPException(409, str(error))
