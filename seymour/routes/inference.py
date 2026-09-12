"""Inference settings' HTTP surface: read, change, reset.

Server-side on purpose (unlike the look-and-feel prefs in localStorage):
these change what every run does, evals must be able to set them, and
the run trace records them — so they live where the runs live.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from seymour import inference

router = APIRouter(prefix="/api/inference")


class InferenceBody(BaseModel):
    """Any subset of the settings; unknown keys are ignored, values are
    validated and clamped server-side (the UI shows the bounds)."""

    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    min_p: float | None = None
    repeat_penalty: float | None = None
    presence_penalty: float | None = None
    thinking: str | None = None
    reasoning_budget: int | None = None
    max_tokens: int | None = None
    history_tokens: int | None = None


def _payload(current: inference.Inference) -> dict:
    return {"settings": current.__dict__ | {},
            "defaults": inference.defaults(), "bounds": inference.bounds()}


@router.get("")
async def get_settings():
    """The effective settings plus factory defaults and bounds."""
    return _payload(inference.current())


@router.post("")
async def set_settings(body: InferenceBody):
    """Change settings; applies to the next request."""
    return _payload(inference.save(body.model_dump(exclude_none=True)))


@router.post("/reset")
async def reset_settings():
    """Back to the model card's recommendations."""
    return _payload(inference.save(inference.defaults()))
