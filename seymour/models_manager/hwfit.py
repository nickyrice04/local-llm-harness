"""Hardware fit: will this model actually run well on THIS machine?

A compact port of Odysseus's cookbook sizing (see ACKNOWLEDGMENTS.md).
The one formula that matters:

    memory_gb = weights_gb                      (the file size — measured!)
              + 8e-6 · active_params_B · ctx    (the KV cache)
              + 0.5                             (runtime overhead)

Two Apple Silicon specifics carried over: the GPU can only address a
FRACTION of unified memory by default (~67% ≤16 GB, 75% ≤64 GB, 80%
above — Apple's own working-set limits), and that fraction — not total
RAM — is the honest budget.

Fit levels are ratio buckets of required/budget:
    ≤ 0.50 → "perfect"   (loads with lots of headroom)
    ≤ 0.78 → "good"      (comfortable)
    ≤ 1.00 → "tight"     (loads, but the machine will feel it)
    > 1.00 → "too_big"   (don't)
"""

import logging
import os
import re
import subprocess

from seymour.config import settings

logger = logging.getLogger(__name__)

# KV cache per token per billion ACTIVE params, in GB (the Odysseus
# constant — empirically calibrated across llama.cpp models).
KV_GB_PER_TOKEN_PER_B = 8e-6
# Flat runtime overhead (buffers, graph, scratch), in GB.
OVERHEAD_GB = 0.5

# Cached hardware probe (RAM doesn't change while we run).
_system: dict | None = None


def system_info() -> dict:
    """Total RAM and the honest GPU-addressable budget, probed once."""
    global _system
    if _system is not None:
        return _system
    total_gb = _total_ram_gb()
    # Apple Silicon: the Metal working-set limit is a FRACTION of unified
    # memory (these track Apple's defaults). On other platforms we treat
    # available RAM as the budget — llama.cpp will happily use CPU+RAM.
    if total_gb <= 16:
        fraction = 0.67
    elif total_gb <= 64:
        fraction = 0.75
    else:
        fraction = 0.80
    _system = {
        "total_ram_gb": round(total_gb, 1),
        "budget_gb": round(total_gb * fraction, 1),
        "budget_note": f"~{int(fraction * 100)}% of unified memory "
                       f"(the GPU's default working-set limit)",
    }
    return _system


def _total_ram_gb() -> float:
    """Physical RAM in GB: sysconf where it works, sysctl on macOS."""
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return pages * page_size / 1024 ** 3
    except (ValueError, OSError, AttributeError):
        pass
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"],
                             capture_output=True, text=True, timeout=2)
        return int(out.stdout.strip()) / 1024 ** 3
    except Exception:
        return 16.0                    # a conservative guess beats a crash


def parse_params(name: str) -> tuple[float, float]:
    """(total_B, active_B) parsed from a model filename.

    Handles the MoE convention: 'Qwen3.6-35B-A3B' means 35B total with 3B
    ACTIVE — only the active params generate KV traffic. A plain '7B'
    is dense: active == total. Unparseable names return (0, 0) and the
    caller reports 'unknown' rather than guessing.
    """
    lowered = name.lower()
    # The MoE marker: '-a3b' / '-a22b' after the total.
    moe = re.search(r"[-_]a(\d+(?:\.\d+)?)b", lowered)
    total = re.search(r"(\d+(?:\.\d+)?)\s*b(?![a-z0-9])", lowered)
    if not total:
        return 0.0, 0.0
    total_b = float(total.group(1))
    active_b = float(moe.group(1)) if moe else total_b
    return total_b, active_b


def fit_for(size_bytes: int, name: str, ctx: int | None = None,
            kv_bytes_per_token: int | None = None, sequences: int = 1,
            extra_gb: float = 0.0) -> dict:
    """The fit verdict for one model file on this machine.

    `size_bytes` is the WEIGHTS number — measured from the actual file (or
    the HF listing), never estimated from a quant table when the truth is
    one stat() away. `kv_bytes_per_token`, when the caller read it from
    the checkpoint's own config (engine/mlxinfo.py), replaces the
    params-B rule of thumb — on hybrid models the rule overcounts ~4x.
    """
    info = system_info()
    ctx = ctx or settings.ctx_size
    weights_gb = size_bytes / 1024 ** 3
    _, active_b = parse_params(name)
    # The KV term needs the active params; without them we still know the
    # weights and say so (a partial answer beats a made-up one).
    if kv_bytes_per_token:
        # Each concurrent sequence keeps its own cache (MLX has no shared
        # pool like --kv-unified), plus a fixed ~150 MB recurrent state on
        # hybrid models; `extra_gb` carries the prompt cache and a drafter.
        kv_gb = (kv_bytes_per_token * ctx / 1024 ** 3 + 0.15) * max(sequences, 1)
        active_b = active_b or 1.0            # "known" below: the config said so
    else:
        kv_gb = KV_GB_PER_TOKEN_PER_B * active_b * ctx if active_b else 0.0
    required = weights_gb + kv_gb + extra_gb + OVERHEAD_GB
    ratio = required / max(info["budget_gb"], 1.0)
    if ratio <= 0.50:
        level = "perfect"
    elif ratio <= 0.78:
        level = "good"
    elif ratio <= 1.00:
        level = "tight"
    else:
        level = "too_big"
    return {
        "level": level,
        "required_gb": round(required, 1),
        "budget_gb": info["budget_gb"],
        "kv_known": bool(active_b),    # False = params unparsed; KV omitted
    }
