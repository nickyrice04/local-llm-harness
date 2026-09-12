"""The MLX config reader: facts from config.json, drafter pairing by
config (never by name alone), and the hybrid-model KV arithmetic."""

import json
from pathlib import Path

from seymour.engine import mlxinfo


def _make_model(root: Path, name: str, *, bits=8, mtp=False, layer_types=None,
                total_size=1000, vision=True) -> Path:
    d = root / name
    d.mkdir()
    text = {"model_type": "qwen3_5_text", "num_hidden_layers": 8, "hidden_size": 512,
            "num_attention_heads": 8, "num_key_value_heads": 2, "head_dim": 64,
            "max_position_embeddings": 4096}
    if layer_types is not None:
        text["layer_types"] = layer_types
    config = {"model_type": "qwen3_5_mtp" if mtp else "qwen3_5", "text_config": text,
              "quantization": {"bits": bits, "group_size": 64, "mode": "affine"}}
    if mtp:
        config["block_size"] = 3
    if vision:
        config["vision_config"] = {"depth": 1}
    (d / "config.json").write_text(json.dumps(config))
    (d / "model.safetensors").write_bytes(b"\0" * 10)
    (d / "model.safetensors.index.json").write_text(json.dumps({"metadata": {"total_size": total_size}, "weight_map": {}}))
    (d / "README.md").write_text("converted with mlx-vlm version 0.6.8")
    return d


def test_reads_facts_and_hybrid_kv(tmp_path):
    d = _make_model(tmp_path, "Q-8bit", layer_types=["linear_attention"] * 6 + ["full_attention"] * 2)
    info = mlxinfo.read_info(d)
    assert info.model_type == "qwen3_5" and info.text_model_type == "qwen3_5_text"
    assert not info.is_drafter and info.quant_bits == 8 and info.quant_mode == "affine"
    assert info.num_layers == 8 and info.kv_layers == 2           # only full-attention layers grow
    assert info.kv_bytes_per_token == 2 * 2 * 2 * 64 * 2            # K+V × layers × heads × dim × bf16
    assert info.max_context == 4096 and info.has_vision and info.converted_by == "mlx-vlm"
    assert info.weight_bytes == 1000 and info.warnings == ()


def test_qwen38_numbers_match_the_real_config():
    real = Path(__file__).resolve().parent.parent / "Models" / "Qwen3.8-27B-8bit"
    if not real.is_dir():
        return                                             # not on this machine
    info = mlxinfo.read_info(real)
    assert info.kv_layers == 16 and info.num_kv_heads == 4 and info.head_dim == 256
    assert info.kv_bytes_per_token == 65536                # 64 KB/token, measured from config
    assert info.max_context == 262144 and info.weight_bytes > 29_000_000_000


def test_drafter_pairing_by_config_not_name(tmp_path):
    base = _make_model(tmp_path, "Q-8bit")
    _make_model(tmp_path, "Q-MTP-8bit", mtp=True)          # the right one
    _make_model(tmp_path, "Q-MTP-4bit", mtp=True, bits=4)  # wrong quant
    _make_model(tmp_path, "Other-8bit")                    # a base model, not a drafter
    (tmp_path / "junk").mkdir()
    drafter = mlxinfo.find_drafter(base)
    assert drafter is not None and drafter.name == "Q-MTP-8bit"
    d = mlxinfo.read_info(drafter)
    assert d.is_drafter and d.draft_block_size == 3 and not d.has_vision
    # A drafter never gets a drafter; a base with no compatible sibling gets None.
    assert mlxinfo.find_drafter(drafter) is None
    assert mlxinfo.find_drafter(_make_model(tmp_path, "Solo-6bit", bits=6)) is None


def test_is_mlx_model_dir(tmp_path):
    assert not mlxinfo.is_mlx_model_dir(tmp_path)          # no config
    d = _make_model(tmp_path, "M-4bit", bits=4)
    assert mlxinfo.is_mlx_model_dir(d)
    (tmp_path / "file.gguf").write_bytes(b"GGUF")
    assert not mlxinfo.is_mlx_model_dir(tmp_path / "file.gguf")
