"""Reading an MLX checkpoint's own config — facts, not filename guesses.

The GGUF twin of this file (engine/gguf.py) reads a binary header; an MLX
model is a DIRECTORY (config.json + *.safetensors + tokenizer files), and
its config.json answers the same questions the registry and the fit check
ask before anything is loaded:

    is this a servable model at all, or a draft-only companion?
    which architecture, how quantized, how much context can it hold?
    how many bytes of KV cache does each token cost on THIS model?
    does a matching MTP drafter sit next to it?

Why the KV question needs the config and not a params-B rule of thumb:
Qwen3.5/3.6/3.8 are HYBRID models — only every Nth layer is full
attention and keeps a growing key/value cache; the other layers are
linear attention with a fixed-size recurrent state. A dense-27B rule of
thumb would overcount by ~4x. Measured on Qwen3.8-27B: 64 layers, 16 of
them full attention, 4 KV heads × 256 head dim → ~64 KB per token.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Bytes per KV element: mlx-lm keeps the cache in the model's compute
# dtype (bf16/fp16 → 2 bytes) unless a quantized KV cache is requested.
_KV_BYTES_PER_ELEMENT = 2


@dataclass(frozen=True)
class MlxInfo:
    """What one MLX model directory says about itself."""

    path: Path
    model_type: str                 # e.g. "qwen3_5" (the wrapper) …
    text_model_type: str            # … and "qwen3_5_text" (the language part)
    is_drafter: bool                # an MTP drafter: NOT a standalone model
    draft_block_size: int           # tokens the drafter proposes per step
    quant_bits: int                 # 8 / 4 / 6 … or 16 for unquantized
    quant_mode: str                 # "affine", "mxfp4", … or "" (unquantized)
    num_layers: int
    kv_layers: int                  # layers that keep a growing KV cache
    num_kv_heads: int
    head_dim: int
    max_context: int                # max_position_embeddings (the ceiling)
    has_vision: bool                # a vision tower is present in the weights
    converted_by: str               # "mlx-vlm" / "mlx-lm" / "" (from README)
    weight_bytes: int               # from the safetensors index, else disk
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def kv_bytes_per_token(self) -> int:
        """Growing cache cost per token: K and V for every KV-keeping layer."""
        return 2 * self.kv_layers * self.num_kv_heads * self.head_dim * _KV_BYTES_PER_ELEMENT

    @property
    def display_name(self) -> str:
        return self.path.name


def is_mlx_model_dir(path: Path) -> bool:
    """A directory with a config.json and at least one safetensors shard
    is an MLX checkpoint candidate. (A drafter passes too — the reader
    then says it is not standalone.)"""
    return (path.is_dir() and (path / "config.json").is_file()
            and any(path.glob("*.safetensors")))


def parse_config(config: dict) -> dict:
    """The facts a config.json states, as a plain dict — pure, so the
    pre-download fit check (which fetches only config.json) and
    read_info share one reading of the format."""
    warnings: list[str] = []
    text = config.get("text_config") if isinstance(config.get("text_config"), dict) else config
    model_type = str(config.get("model_type") or text.get("model_type") or "unknown")
    text_type = str(text.get("model_type") or model_type)
    is_drafter = model_type.endswith("_mtp") or bool(config.get("is_draft_model"))
    block_size = int(config.get("block_size") or config.get("num_nextn_predict_layers") or 0)
    quant = config.get("quantization") or config.get("quantization_config") or {}
    quant_bits = int(quant.get("bits") or 16) if isinstance(quant, dict) else 16
    quant_mode = str(quant.get("mode") or ("affine" if quant else "")) if isinstance(quant, dict) else ""
    num_layers = int(text.get("num_hidden_layers") or 0)
    layer_types = text.get("layer_types")
    if isinstance(layer_types, list) and layer_types:
        kv_layers = sum(1 for kind in layer_types if "full" in str(kind))
    elif text.get("full_attention_interval"):
        kv_layers = num_layers // int(text["full_attention_interval"])
    else:
        kv_layers = num_layers
    num_kv_heads = int(text.get("num_key_value_heads") or text.get("num_attention_heads") or 0)
    head_dim = int(text.get("head_dim") or 0)
    if not head_dim and text.get("hidden_size") and text.get("num_attention_heads"):
        head_dim = int(text["hidden_size"]) // int(text["num_attention_heads"])
    if not (num_layers and num_kv_heads and head_dim):
        warnings.append("KV size unknown: config lacks layer/head fields")
    max_context = int(text.get("max_position_embeddings") or config.get("max_position_embeddings") or 0)
    return {
        "model_type": model_type, "text_model_type": text_type, "is_drafter": is_drafter,
        "draft_block_size": block_size, "quant_bits": quant_bits, "quant_mode": quant_mode,
        "num_layers": num_layers, "kv_layers": kv_layers, "num_kv_heads": num_kv_heads,
        "head_dim": head_dim, "max_context": max_context,
        "has_vision": isinstance(config.get("vision_config"), dict) and not is_drafter,
        "hidden_size": int(text.get("hidden_size") or 0),
        "kv_bytes_per_token": 2 * kv_layers * num_kv_heads * head_dim * _KV_BYTES_PER_ELEMENT,
        "warnings": warnings,
    }


def read_info(path: Path) -> MlxInfo:
    """Read config.json (+ the safetensors index and README) — no weights
    are touched, so this is cheap enough to call on every list."""
    path = Path(path)
    try:
        config = json.loads((path / "config.json").read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{path.name}: unreadable config.json ({error})") from error
    facts = parse_config(config)
    warnings: list[str] = list(facts["warnings"])
    # Weight bytes: the safetensors index carries the exact total; a
    # single-file checkpoint (drafters, small models) has no index.
    weight_bytes = 0
    index = path / "model.safetensors.index.json"
    if index.is_file():
        try:
            weight_bytes = int(json.loads(index.read_text()).get("metadata", {}).get("total_size") or 0)
        except (OSError, json.JSONDecodeError, ValueError):
            weight_bytes = 0
    if not weight_bytes:
        weight_bytes = sum(p.stat().st_size for p in path.glob("*.safetensors"))

    converted_by = ""
    readme = path / "README.md"
    if readme.is_file():
        try:
            head = readme.read_text(errors="replace")[:4000]
            if "mlx-vlm" in head or "mlx_vlm" in head:
                converted_by = "mlx-vlm"
            elif "mlx-lm" in head or "mlx_lm" in head:
                converted_by = "mlx-lm"
        except OSError:
            pass
    return MlxInfo(
        path=path, model_type=facts["model_type"], text_model_type=facts["text_model_type"],
        is_drafter=facts["is_drafter"], draft_block_size=facts["draft_block_size"],
        quant_bits=facts["quant_bits"], quant_mode=facts["quant_mode"],
        num_layers=facts["num_layers"], kv_layers=facts["kv_layers"],
        num_kv_heads=facts["num_kv_heads"], head_dim=facts["head_dim"],
        max_context=facts["max_context"], has_vision=facts["has_vision"],
        converted_by=converted_by, weight_bytes=weight_bytes, warnings=tuple(warnings),
    )


def find_drafter(model_dir: Path, candidates: list[Path] | None = None) -> Path | None:
    """The MTP drafter that belongs to this model, if one sits nearby.

    A drafter is a sibling directory whose config says `<type>_mtp` for
    the same text model type AND the same quantization (a 4-bit drafter
    cannot serve an 8-bit target: mlx-vlm checks the vocab, we check the
    rest). Name conventions (`<name>-MTP-<quant>`) are used only to RANK
    candidates, never to decide — the config decides.
    """
    model_dir = Path(model_dir)
    try:
        base = read_info(model_dir)
    except ValueError:
        return None
    if base.is_drafter:
        return None
    if candidates is None:
        # Siblings first; then every checkpoint under the models folder,
        # hub snapshots included — a hub snapshot's siblings are other
        # revisions of ITSELF, so its drafter lives two levels up.
        from seymour.config import settings
        pool = [p for p in model_dir.parent.iterdir() if p.is_dir() and p != model_dir]
        root = settings.models_dir
        if root.is_dir():
            pool += [p for p in root.iterdir() if p.is_dir() and p != model_dir]
            pool += [p for p in (root / "hub").glob("models--*/snapshots/*") if p != model_dir]
    else:
        pool = candidates
    matches: list[tuple[int, Path]] = []
    for candidate in pool:
        if not is_mlx_model_dir(candidate):
            continue
        try:
            info = read_info(candidate)
        except ValueError:
            continue
        if not info.is_drafter or info.text_model_type != base.text_model_type:
            continue
        if info.quant_bits != base.quant_bits:
            continue
        # Prefer the conventional sibling name; any config-compatible
        # drafter still qualifies.
        stem = model_dir.name.lower().replace("-mtp", "")
        rank = 0 if candidate.name.lower().replace("-mtp", "") == stem else 1
        matches.append((rank, candidate))
    if not matches:
        return None
    matches.sort(key=lambda pair: (pair[0], pair[1].name))
    return matches[0][1]
