"""The local model registry: what's on disk, and swapping the active model.

Any GGUF file OR MLX checkpoint directory is a candidate brain for
Seymour. The registry finds them in two places — loose entries in Models/,
and the Hugging Face hub cache the downloader fills (Models/hub/
models--org--name/snapshots/…) — and knows which one the engine is
currently serving. The entry's KIND decides the engine: a .gguf launches
llama-server, a directory with config.json + safetensors launches an MLX
server (engine/mlx.py). Same handshake, same scheduler either way.

Swapping models is the heavyweight operation in this file: stop the engine
(frees ~35 GB), start it on the new weights, re-run the FULL handshake
(capabilities are a property of the (build, model, flags, machine)
combination — a new model can change every answer), and rebuild the
scheduler on the fresh measurements.
"""

import asyncio
import contextlib
import logging
from pathlib import Path

from seymour import runtime
from seymour.config import settings
from seymour.db import get_state, set_state
from seymour.events import bus
from seymour.engine import mlxinfo
from seymour.models_manager import hwfit

logger = logging.getLogger(__name__)

# One lock so two activate calls can't interleave engine restarts.
_swap_lock = asyncio.Lock()


def _human_size(num_bytes: int) -> str:
    """Bytes → '35.2 GB' (display only)."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# GGUF headers don't change under us, so read each file at most once.
_mtp_cache: dict[str, bool] = {}


def _has_mtp(path: Path) -> bool:
    """True when these weights ship MTP draft heads (cached per path)."""
    key = str(path)
    if key not in _mtp_cache:
        from seymour.engine.gguf import read_info
        _mtp_cache[key] = read_info(path).supports_mtp
    return _mtp_cache[key]


def active_model_path() -> Path:
    """The weights the engine should serve (a .gguf file or an MLX
    directory): the user's saved choice, falling back to the default."""
    saved = get_state("active_model")
    return Path(saved) if saved else settings.model_path


def model_kind(path: Path) -> str:
    """'mlx' for a checkpoint directory, 'gguf' for a file. The kind — not
    a setting — decides which engine serves it."""
    return "mlx" if Path(path).is_dir() else "gguf"


def _mlx_candidates() -> list[Path]:
    """MLX checkpoint directories: loose in Models/ (hf download
    --local-dir, or a copied folder) and inside the hub cache's snapshot
    folders (what the downloader writes)."""
    found: list[Path] = []
    if settings.models_dir.is_dir():
        found += [p for p in settings.models_dir.iterdir() if mlxinfo.is_mlx_model_dir(p)]
    hub = settings.models_dir / "hub"
    if hub.is_dir():
        found += [p for p in hub.glob("models--*/snapshots/*") if mlxinfo.is_mlx_model_dir(p)]
    return found


def _mlx_rows(active: str | None) -> list[dict]:
    """One row per servable MLX directory. Drafters are NOT rows — they
    are companions, shown on the row of the model they serve."""
    rows: list[dict] = []
    candidates = _mlx_candidates()
    # Fit is judged at the context the NEXT load will actually use (the
    # saved engine setting), not the config default.
    chosen = engine_settings()
    ctx = chosen["ctx_size"]
    for path in sorted(candidates):
        try:
            info = mlxinfo.read_info(path)
        except ValueError as error:
            logger.warning("skipping %s: %s", path, error)
            continue
        if info.is_drafter:
            continue
        drafter = mlxinfo.find_drafter(path, candidates)
        # The hub layout names a snapshot by its commit hash; the repo
        # name two levels up is what a person recognises.
        name = path.name
        if path.parent.name == "snapshots":
            name = path.parent.parent.name.replace("models--", "").replace("--", "/")
        rows.append({
            "name": name,
            "path": str(path),
            "size": _human_size(info.weight_bytes),
            "active": name == active,
            "backend": "mlx",
            "fit": hwfit.fit_for(
                info.weight_bytes, name, ctx=ctx,
                kv_bytes_per_token=info.kv_bytes_per_token or None,
                sequences=int(chosen["mlx_decode_concurrency"]),
                extra_gb=float(chosen["mlx_prompt_cache_gb"])
                         + (mlxinfo.read_info(drafter).weight_bytes / 1024 ** 3 if drafter else 0.0)),
            # MTP here means "a compatible drafter checkpoint sits next to
            # these weights" — the engine turns it on only when asked, and
            # the handshake measures whether it actually drafts.
            "mtp": drafter is not None,
            "drafter": drafter.name if drafter else None,
            "quant": f"{info.quant_bits}-bit {info.quant_mode}".strip() if info.quant_bits < 16 else "bf16",
            "context_max": info.max_context,
            "vision": info.has_vision,
            "notes": list(info.warnings) + ([f"converted with {info.converted_by}"] if info.converted_by else []),
        })
    return rows


def list_local_models() -> list[dict]:
    """Every servable model we can find — .gguf files and MLX directories
    — with the LOADED one flagged.

    'Active' means the engine is actually serving it right now — not
    merely that it is the saved choice. After an Unload, nothing is
    active, and the list says so truthfully.
    """
    active = runtime.engine.model_name if runtime.engine is not None else None
    found: list[dict] = _mlx_rows(active)
    ctx = engine_settings()["ctx_size"]
    # Loose files in Models/ (the hand-downloaded case)…
    candidates = list(settings.models_dir.glob("*.gguf"))
    # …plus everything inside the HF hub cache the downloader maintains.
    candidates += list((settings.models_dir / "hub").rglob("*.gguf"))
    for path in sorted(set(candidates)):
        # mmproj files are vision projectors, not servable models.
        if "mmproj" in path.name.lower():
            continue
        # Split-model shards: only the first shard is passed to llama-server
        # (it finds its siblings itself), so hide the rest from the list.
        if "-of-" in path.name and "-00001-of-" not in path.name:
            continue
        stat = path.stat()
        found.append({
            "name": path.name,
            "path": str(path),
            "size": _human_size(stat.st_size),
            "active": path.name == active,
            "backend": "gguf",
            # Will it run WELL here? Computed from the real file size +
            # this machine's measured memory budget (hwfit.py).
            "fit": hwfit.fit_for(stat.st_size, path.name, ctx=ctx),
            # Does this FILE carry MTP draft heads? Read from its own
            # header, so the list can show which models get the
            # self-speculative speedup before you load one.
            "mtp": _has_mtp(path),
        })
    return found


async def activate_model(path: str) -> dict:
    """Load a model: swap the engine onto different weights. Slow (a full
    model load) and announced honestly on the bus at every stage."""
    # An unload mid-drain owns the engine's fate; loading now would race
    # it. The user resolves the fork explicitly (cancel the unload, or
    # let it finish first).
    if unload_in_progress():
        raise ValueError("an unload is in progress — cancel it or wait "
                         "for it to finish before loading a model")
    target = Path(path)
    # Only serve files the registry itself can see — this is also the
    # security boundary that stops the API loading arbitrary host paths.
    known = {m["path"] for m in list_local_models()}
    if str(target) not in known:
        raise ValueError(f"not a known local model: {path}")

    async with _swap_lock:
        # Imports here to avoid module-level cycles (registry ↔ engine).
        from seymour.engine.llamacpp import LlamaCppEngine
        from seymour.engine.mlx import MlxEngine
        from seymour.scheduler.core import Scheduler
        kind = model_kind(target)

        # The adopted-server trap (the "Load does nothing" bug): when the
        # running engine is one Seymour merely ADOPTED, its port belongs
        # to someone else's server. We cannot kill it, and starting our
        # own would just re-adopt it — so loading the SAME model is a
        # no-op success, and loading a DIFFERENT one is refused with the
        # remedy named, BEFORE anything gets torn down.
        if runtime.engine is not None and runtime.engine.external:
            if runtime.engine.model_name == target.name:
                return {"model": target.name, "note": "already being served",
                        "mode": runtime.scheduler.policy.mode}
            raise ValueError(
                f"the current model ({runtime.engine.model_name}) is served "
                f"by a server Seymour did not start. Stop that server "
                f"(or change its port), then load {target.name}."
            )

        bus.publish("engine", "swapping", model=target.name)
        # 1. Stop the old engine — releases its memory before the new load.
        if runtime.engine is not None:
            await runtime.engine.stop()
        # The old engine is gone either way, so say so in the shared
        # runtime NOW. If the new load fails below, the app is left in
        # "no model loaded" — a state every route already answers with an
        # honest 503 — instead of routes silently calling a stopped
        # engine's closed HTTP client (which 500s in confusing ways).
        runtime.engine = None
        runtime.caps = None
        runtime.scheduler = None
        # 2. Start the new one (blocks until the weights are resident).
        #    adopt="same-model": an explicit Load must end up serving the
        #    CHOSEN weights — a foreign server already serving exactly
        #    those weights counts (instant load); anything else fails
        #    loudly instead of silently keeping the wrong model.
        # The KIND of weights picks the engine; everything after this
        # line is identical for both (start, handshake, scheduler).
        engine = (MlxEngine(model_path=target) if kind == "mlx"
                  else LlamaCppEngine(model_path=target))
        try:
            await engine.start(adopt="same-model")
            # 3. Re-measure everything. A different model may batch
            #    differently, cache differently, or support different
            #    features.
            caps = await engine.capabilities()
        except Exception as error:
            # The load or the handshake failed (corrupt file, OOM,
            # timeout). Stop whatever half-started — never leak a
            # llama-server child — tell the UI, and give the route a
            # typed error to map to a clean HTTP response.
            with contextlib.suppress(Exception):
                await engine.stop()
            bus.publish("engine", "swap_failed", model=target.name,
                        error=str(error)[:300])
            logger.exception("model swap to %s failed", target.name)
            raise RuntimeError(f"could not load {target.name}: {error}") from error
        # 4. Rebuild the scheduler on the fresh measurements and republish.
        runtime.engine = engine
        runtime.caps = caps
        runtime.scheduler = Scheduler(engine, caps)
        # 5. Persist the choice for next launch — and re-arm autoload
        #    (an explicit Load overrides any earlier Unload).
        set_state("active_model", str(target))
        set_state("engine_autoload", "yes")
        bus.publish("engine", "swapped", model=target.name,
                    mode=runtime.scheduler.policy.mode)
        logger.info("model swapped to %s (mode=%s)", target.name,
                    runtime.scheduler.policy.mode)
        return {"model": target.name, "mode": runtime.scheduler.policy.mode}


# The unload-in-progress task (bug 4.2): unload is a REQUEST, not a
# command — with live work it drains (or cancels) first and only reports
# success at refcount zero. This handle is how "cancel the unload" works.
_drain_task: "asyncio.Task | None" = None


def unload_in_progress() -> bool:
    """True while a drain/cancel unload is still waiting for work."""
    return _drain_task is not None and not _drain_task.done()


async def _stop_engine_now() -> dict:
    """The actual unload — callers guarantee refcount zero (or forced).
    Must be called under _swap_lock."""
    name = runtime.engine.model_name
    was_external = runtime.engine.external
    await runtime.engine.stop()
    runtime.engine = None
    runtime.caps = None
    runtime.scheduler = None
    # Remember the choice: the next boot must NOT auto-load the model
    # the user just deliberately unloaded.
    set_state("engine_autoload", "no")
    bus.publish("engine", "unloaded", model=name, external=was_external)
    logger.info("model unloaded (%s%s)", name,
                " — external server left running" if was_external else "")
    return {"unloaded": True, "model": name, "external": was_external}


async def _finish_unload(mode: str) -> None:
    """Background: wait for refcount zero, then stop the engine.

    Drain waits as long as the work runs (the user holds a cancel-unload
    button); cancel-and-unload waits briefly — the aborts were already
    sent, and a task that ignores cancellation for 15 s must not hold
    the engine hostage forever (logged loudly, then forced).
    """
    scheduler = runtime.scheduler
    if scheduler is not None:
        if mode == "cancel":
            if not await scheduler.wait_idle(timeout=15.0):
                logger.warning("cancel-and-unload: work still live after "
                               "15s of cancellation — forcing the stop")
        else:
            await scheduler.wait_idle()
    async with _swap_lock:
        if runtime.engine is None:
            return                     # someone beat us to it
        await _stop_engine_now()


async def deactivate_model(mode: str = "check") -> dict:
    """Unload the model — as a REQUEST that respects live work (4.2).

    Modes:
      "check"  — unload only if idle; if busy, report WHAT is running so
                 the UI can offer the real choices (never a silent kill).
      "drain"  — block new work, let in-flight work finish, unload at
                 refcount zero (the default choice in the UI).
      "cancel" — pause/cancel the work (agents pause resumably, research
                 keeps its draft, chat streams abort for real), then
                 unload.
    Success ("unloaded": true) is only ever reported at refcount zero.
    """
    global _drain_task
    async with _swap_lock:
        if runtime.engine is None:
            return {"unloaded": False, "note": "nothing is loaded"}
        if unload_in_progress():
            return {"unloaded": False, "draining": True,
                    "note": "an unload is already in progress"}
        scheduler = runtime.scheduler
        snapshot = scheduler.snapshot() if scheduler else {}
        live = snapshot.get("active", []) + snapshot.get("queued", [])
        if not live or scheduler is None:
            # Idle: the simple, instant path.
            return await _stop_engine_now()
        if mode == "check":
            # Busy: report the truth and make the user choose.
            return {"unloaded": False, "busy": True,
                    "active": [t["label"] for t in snapshot.get("active", [])],
                    "queued": [w["label"] for w in snapshot.get("queued", [])]}
        # A real unload begins: from here, no new work is admitted.
        scheduler.begin_drain()

    if mode == "cancel":
        # Graceful first — the managers know how to stop their own work
        # (agents pause resumably, research keeps drafts + writes its
        # outcome into the conversation)…
        from seymour.agent.discrete import discrete
        from seymour.research import research
        await discrete.pause_all()
        await research.cancel_all()
        if runtime.agent is not None:
            await runtime.agent.pause_all()
        # …then abort whatever still holds or awaits a slot (chat
        # streams, memory/title one-shots): cancelling the consumer
        # closes the engine's HTTP stream, which is what actually makes
        # llama-server stop decoding and free the slot.
        scheduler.abort_active()

    _drain_task = asyncio.create_task(_finish_unload(mode))
    bus.publish("engine", "draining", mode=mode, waiting_for=len(live))
    return {"unloaded": False, "draining": True, "mode": mode,
            "waiting_for": len(live)}


async def cancel_unload() -> dict:
    """The third choice: keep the model — stop the drain, admit work again."""
    global _drain_task
    if not unload_in_progress():
        return {"cancelled": False, "note": "no unload is in progress"}
    _drain_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _drain_task
    _drain_task = None
    if runtime.scheduler is not None:
        runtime.scheduler.end_drain()
    bus.publish("engine", "drain_cancelled")
    logger.info("unload cancelled — admissions reopened")
    return {"cancelled": True}


# ---- Engine settings the UI may change ------------------------------------ #
# These four decide how the NEXT engine launch is configured. They live in
# app_state (not .env) because they are product choices a person makes in
# the Models tab, and they must survive restarts. Each falls back to the
# config default, so an untouched install behaves exactly as before.
_ENGINE_KEYS = {
    # Shared: MTP on/off and the context budget mean the same thing to
    # both engines (for MLX, ctx_size caps a request's window).
    "mtp": ("engine_mtp", lambda: settings.mtp),
    "ctx_size": ("engine_ctx_size", lambda: settings.ctx_size),
    # llama.cpp only.
    "mtp_draft_n": ("engine_mtp_draft_n", lambda: settings.mtp_draft_n),
    "n_slots": ("engine_n_slots", lambda: settings.n_slots),
    # MLX only (engine/mlx.py reads these at launch).
    "mlx_server": ("engine_mlx_server", lambda: settings.mlx_server),
    "mlx_decode_concurrency": ("engine_mlx_decode_concurrency", lambda: settings.mlx_decode_concurrency),
    "mlx_prompt_concurrency": ("engine_mlx_prompt_concurrency", lambda: settings.mlx_prompt_concurrency),
    "mlx_prompt_cache_gb": ("engine_mlx_prompt_cache_gb", lambda: settings.mlx_prompt_cache_gb),
    "mlx_draft_tokens": ("engine_mlx_draft_tokens", lambda: settings.mlx_draft_tokens),
}


def engine_settings() -> dict:
    """The effective engine settings: saved overrides, else config defaults."""
    values = {}
    for name, (key, default) in _ENGINE_KEYS.items():
        saved = get_state(key)
        fallback = default()
        if saved is None:
            values[name] = fallback
        elif isinstance(fallback, int):
            try:
                values[name] = int(saved)
            except ValueError:
                values[name] = fallback
        else:
            values[name] = saved
    return values


def save_engine_settings(changes: dict) -> dict:
    """Persist engine settings. They take effect on the NEXT load — the
    caller says so plainly rather than pretending a running server
    changed its launch flags."""
    for name, value in changes.items():
        if name not in _ENGINE_KEYS or value is None:
            continue
        key, _ = _ENGINE_KEYS[name]
        set_state(key, str(value))
    return engine_settings()
