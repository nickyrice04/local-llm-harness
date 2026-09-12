"""Status: the honesty layer's HTTP surface.

GET /api/status answers, in one call, the question the whole project
exists to answer: what is this machine actually doing right now, and why?
The mode banner, the slot meter, and the first-launch onboarding flag all
come from here.
"""

import re
import subprocess
import time

from fastapi import APIRouter

from seymour import runtime
from seymour.agent import power
from seymour.db import get_state, set_state

router = APIRouter(prefix="/api")


@router.get("/status")
async def status():
    """Everything the header UI needs, from the real components."""
    # The frozen handshake measurements (None only before startup ends).
    caps = runtime.caps
    # The scheduler's live snapshot: mode, held slots, shares, waiters.
    snapshot = runtime.scheduler.snapshot() if runtime.scheduler else {}
    # The engine's live slot occupancy.
    stats = await runtime.engine.stats() if runtime.engine else None
    return {
        "capabilities": caps.__dict__ if caps else None,
        "scheduler": snapshot,
        "engine": {
            "slots_total": stats.slots_total if stats else 0,
            "slots_busy": stats.slots_busy if stats else 0,
            "slots": stats.slots if stats else [],
        },
        "battery": {
            "on_battery": power.on_battery(),
            "percent": power.battery_percent(),
        },
        # System memory + GPU: on Apple Silicon, unified memory IS the
        # GPU's memory, so together these two numbers are the honest
        # "how hard is the machine working" gauge the health panel shows.
        "system": {**_memory_info(), **_gpu_info()},
        # MTP: what the weights offer vs what this server is actually
        # doing. The gap matters — an ADOPTED server started without
        # --spec-type can't use heads the file ships, and the honest
        # move is to say so with the remedy (the vision-projector
        # lesson applied to speed).
        "mtp": _mtp_state(),
    }


def _mtp_state() -> dict:
    """Available (the file has MTP heads) vs enabled (this server drafts)."""
    caps = runtime.caps
    engine = runtime.engine
    available = False
    model_path = getattr(engine, "model_path", None)
    if model_path is not None:
        from pathlib import Path
        if Path(model_path).is_dir():
            # MLX: "available" means a compatible drafter checkpoint sits
            # next to the weights (engine/mlxinfo.py decides by config).
            from seymour.engine import mlxinfo
            available = mlxinfo.find_drafter(Path(model_path)) is not None
        else:
            from seymour.engine.gguf import read_info
            available = read_info(model_path).supports_mtp
    return {
        "available": available,
        "enabled": bool(caps and caps.mtp_enabled),
        "acceptance": (caps.mtp_acceptance if caps else 0.0),
        # True when the speed is sitting on the table: the weights can
        # draft, but this server was launched without it.
        "missed": bool(available and caps and not caps.mtp_enabled),
        "external": bool(getattr(engine, "external", False)),
    }


# vm_stat/ioreg results, cached briefly: /api/status is polled every 2 s
# by the health panel, and shelling out on every poll would be waste.
# (ts, value) pairs keyed by monotonic time.
_mem_cache: tuple[float, dict] = (0.0, {})
_gpu_cache: tuple[float, int | None] = (0.0, None)


def _memory_info() -> dict:
    """Used/total RAM in GB, matching Activity Monitor's "Memory Used".

    psutil's (total - available) counts the RECLAIMABLE FILE CACHE as
    used — measured on this machine: 64.2 GB "used" while Activity
    Monitor said 36.9 GB, a 27 GB lie. Activity Monitor counts
    App (anonymous − purgeable) + Wired + Compressed, all readable from
    vm_stat, so compute exactly that; off macOS (or if vm_stat ever
    changes shape) fall back to the psutil approximation.
    """
    global _mem_cache
    ts, cached = _mem_cache
    now = time.monotonic()
    if cached and now - ts < 2.0:
        return cached
    try:
        out = subprocess.run(["vm_stat"], capture_output=True,
                             text=True, timeout=2).stdout
        page = int(re.search(r"page size of (\d+) bytes", out).group(1))

        def pages(label: str) -> int:
            match = re.search(rf"{re.escape(label)}:\s+(\d+)\.", out)
            if match is None:
                raise ValueError(f"vm_stat missing {label!r}")
            return int(match.group(1))

        used = (pages("Anonymous pages") - pages("Pages purgeable")
                + pages("Pages wired down")
                + pages("Pages occupied by compressor")) * page
        import psutil
        total = psutil.virtual_memory().total    # == sysctl hw.memsize
        info = {
            "ram_used_gb": round(used / 1024 ** 3, 1),
            "ram_total_gb": round(total / 1024 ** 3, 1),
            "ram_percent": round(used / total * 100, 1),
        }
    except Exception:
        try:
            import psutil
            vm = psutil.virtual_memory()
            info = {
                "ram_used_gb": round((vm.total - vm.available) / 1024 ** 3, 1),
                "ram_total_gb": round(vm.total / 1024 ** 3, 1),
                "ram_percent": vm.percent,
            }
        except Exception:
            info = {"ram_used_gb": 0, "ram_total_gb": 0, "ram_percent": 0}
    _mem_cache = (now, info)
    return info


def _gpu_info() -> dict:
    """GPU busy % from IOKit's accelerator statistics (no sudo, ~20 ms).

    The Apple-silicon GPU driver publishes a PerformanceStatistics dict
    with "Device Utilization %" — the same number Activity Monitor's GPU
    history graphs. gpu_percent is None when no reading exists (not a
    Mac, or the key vanished in some future driver) — the UI simply
    omits the line rather than showing a made-up zero.
    """
    global _gpu_cache
    ts, value = _gpu_cache
    now = time.monotonic()
    if now - ts < 2.0:
        return {"gpu_percent": value}
    percent: int | None = None
    try:
        out = subprocess.run(
            ["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"],
            capture_output=True, text=True, timeout=2).stdout
        match = re.search(r'"Device Utilization %"=(\d+)', out)
        if match:
            percent = int(match.group(1))
    except Exception:
        percent = None
    _gpu_cache = (now, percent)
    return {"gpu_percent": percent}


@router.get("/onboarding")
async def onboarding_state():
    """Has the first-launch tour been seen? (app_state flag)."""
    return {"done": get_state("onboarding_done") == "yes"}


@router.post("/onboarding/done")
async def onboarding_done():
    """The user finished (or dismissed) the tour — never show it again."""
    set_state("onboarding_done", "yes")
    return {"done": True}
