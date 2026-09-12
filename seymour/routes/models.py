"""Model management's HTTP surface: local list, HF search, download, swap."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from seymour import runtime
from seymour.models_manager import downloader, registry

router = APIRouter(prefix="/api/models")


class ActivateBody(BaseModel):
    """Which local model file to serve next."""

    path: str


class DownloadBody(BaseModel):
    """Which repo to download — and which file, for GGUF repos. An empty
    file means the whole repo (an MLX checkpoint directory)."""

    repo: str
    file: str = ""


@router.get("")
async def local_models():
    """Every servable .gguf on disk, with the active one flagged."""
    return registry.list_local_models()


@router.post("/activate")
async def activate(body: ActivateBody):
    """Load a model: swap the engine onto these weights (slow; progress
    arrives on the event bus — swapping → ready/swap_failed)."""
    try:
        return await registry.activate_model(body.path)
    except ValueError as error:
        # A request the registry refuses outright: unknown path, or the
        # port belongs to a server Seymour didn't start. 409: the state,
        # not the request shape, is what blocks it.
        raise HTTPException(409, str(error))
    except RuntimeError as error:
        # The new model failed to load; the registry already rolled the
        # runtime back to "no model loaded". 502: the upstream engine—not
        # this request—was at fault, and retrying with another model works.
        raise HTTPException(502, str(error))


class UnloadBody(BaseModel):
    """The unload request's mode: "check" (default — unload if idle,
    otherwise report the live work), "drain" (finish current work,
    then unload) or "cancel" (stop the work, then unload)."""

    mode: str = "check"


@router.post("/unload")
async def unload(body: UnloadBody | None = None):
    """Unload the current model — a REQUEST that respects live work
    (4.2): success is only ever reported at refcount zero, and a busy
    engine answers with what is running so the UI can offer drain /
    cancel-work / keep. The choice survives restarts (no auto-reload
    of an unloaded model)."""
    mode = body.mode if body else "check"
    if mode not in ("check", "drain", "cancel"):
        raise HTTPException(422, "mode must be check, drain or cancel")
    return await registry.deactivate_model(mode)


@router.post("/unload/cancel")
async def unload_cancel():
    """The third choice: keep the model loaded — stop the drain and
    admit work again."""
    return await registry.cancel_unload()


class EngineSettings(BaseModel):
    """Engine options a person may change in the Models tab. All optional
    — a request sets only what it wants to change."""

    mtp: str | None = None            # "auto" | "off"  (both engines)
    mtp_draft_n: int | None = None    # llama.cpp: tokens drafted per step
    n_slots: int | None = None        # llama.cpp --parallel: concurrent sequences
    ctx_size: int | None = None       # total context budget (both engines)
    mlx_server: str | None = None     # "auto" | "mlx-lm" | "mlx-vlm"
    mlx_decode_concurrency: int | None = None
    mlx_prompt_concurrency: int | None = None
    mlx_prompt_cache_gb: int | None = None
    mlx_draft_tokens: int | None = None


@router.get("/engine")
async def get_engine_settings():
    """The engine options in force, plus what the LOADED engine actually
    measured — the settings are a request; capabilities are the truth."""
    caps = runtime.caps
    return {
        "settings": registry.engine_settings(),
        "measured": {
            "total_slots": caps.total_slots if caps else 0,
            "concurrent": bool(caps and caps.concurrent),
            "measured_speedup": caps.measured_speedup if caps else 0.0,
            "mtp_enabled": bool(caps and caps.mtp_enabled),
            "mtp_acceptance": caps.mtp_acceptance if caps else 0.0,
            "context_per_slot": caps.context_per_slot if caps else 0,
            # Were the probes run on battery? Then the ratio above is a
            # duty-cycle artifact and the card says so instead of showing it.
            "measured_on_battery": bool(caps and caps.measured_on_battery),
        } if caps else None,
    }


@router.post("/engine")
async def set_engine_settings(body: EngineSettings):
    """Save engine options. They apply on the NEXT load — llama-server's
    launch flags cannot change under a running process, and pretending
    otherwise would be the kind of lie this app exists to avoid."""
    if body.mtp is not None and body.mtp not in ("auto", "off"):
        raise HTTPException(422, "mtp must be auto or off")
    if body.n_slots is not None and not 1 <= body.n_slots <= 16:
        raise HTTPException(422, "n_slots must be between 1 and 16")
    if body.mtp_draft_n is not None and not 1 <= body.mtp_draft_n <= 8:
        raise HTTPException(422, "mtp_draft_n must be between 1 and 8")
    if body.ctx_size is not None and not 2048 <= body.ctx_size <= 1048576:
        raise HTTPException(422, "ctx_size must be between 2048 and 1048576")
    if body.mlx_server is not None and body.mlx_server not in ("auto", "mlx-lm", "mlx-vlm"):
        raise HTTPException(422, "mlx_server must be auto, mlx-lm or mlx-vlm")
    for name, low, high in (("mlx_decode_concurrency", 1, 16), ("mlx_prompt_concurrency", 1, 8),
                            ("mlx_prompt_cache_gb", 0, 96), ("mlx_draft_tokens", 1, 8)):
        value = getattr(body, name)
        if value is not None and not low <= value <= high:
            raise HTTPException(422, f"{name} must be between {low} and {high}")
    saved = registry.save_engine_settings(body.model_dump(exclude_none=True))
    return {"settings": saved, "note": "applies on the next model load"}


@router.post("/remeasure")
async def remeasure():
    """Re-run the handshake against the RUNNING engine and rebuild the
    scheduler from the fresh numbers.

    This exists because the concurrency probe decides concurrent-vs-serial
    for the whole session, and a cold or contended machine can measure
    low once and be stuck at one slot. Re-measuring on a warm engine is
    the honest correction — and it is measurement, not an override.
    """
    if runtime.engine is None:
        raise HTTPException(503, "no model is loaded")
    from seymour.engine.handshake import run_handshake
    from seymour.scheduler.core import Scheduler
    caps = await run_handshake(runtime.engine)
    runtime.caps = caps
    runtime.scheduler = Scheduler(runtime.engine, caps)
    return {"mode": runtime.scheduler.policy.mode,
            "slots": runtime.scheduler.policy.total_slots,
            "measured_speedup": caps.measured_speedup,
            "mtp_enabled": caps.mtp_enabled,
            "mtp_acceptance": caps.mtp_acceptance}


@router.get("/search")
async def search(q: str, backend: str = "gguf"):
    """Search Hugging Face for repos in one backend's format (gguf | mlx)."""
    if not q.strip():
        raise HTTPException(422, "empty query")
    try:
        return await downloader.hf_search(q, backend)
    except ValueError as error:
        raise HTTPException(422, str(error))


@router.get("/files")
async def files(repo: str, backend: str = "gguf"):
    """A repo's downloadable units: .gguf files (the quant picker), or
    the whole repo for MLX (plus its drafter repo when one exists)."""
    try:
        return await downloader.hf_files(repo, backend)
    except ValueError as error:
        raise HTTPException(422, str(error))


@router.post("/download")
async def download(body: DownloadBody):
    """Start a download (progress arrives on /api/events): one GGUF file,
    or a whole MLX repo when `file` is empty."""
    try:
        return downloader.start_download(body.repo, body.file)
    except ValueError as error:
        raise HTTPException(422, str(error))
    except RuntimeError as error:
        raise HTTPException(409, str(error))


@router.post("/download/cancel")
async def cancel_download():
    """Stop the active download (the partial blob remains, resumable)."""
    return {"cancelled": downloader.cancel_download()}


@router.get("/download/status")
async def download_status():
    """Is a download in flight? (UI catch-up on page load.)"""
    return downloader.download_status()
