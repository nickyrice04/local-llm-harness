"""The registry lists MLX folders next to GGUF files, hides drafters,
pairs them by config, and picks the engine by kind."""

import json
from pathlib import Path

import pytest

from seymour.config import settings
from seymour.models_manager import downloader, registry


def _mlx(root: Path, name: str, *, mtp=False, bits=8, layers=None):
    d = root / name; d.mkdir(parents=True)
    text = {"model_type": "qwen3_5_text", "num_hidden_layers": 4, "num_key_value_heads": 2,
            "head_dim": 64, "max_position_embeddings": 8192,
            "layer_types": layers or ["linear_attention", "full_attention"] * 2}
    cfg = {"model_type": "qwen3_5_mtp" if mtp else "qwen3_5", "text_config": text,
           "quantization": {"bits": bits, "mode": "affine"}, "block_size": 3}
    if not mtp:
        cfg["vision_config"] = {}
    (d / "config.json").write_text(json.dumps(cfg))
    (d / "model.safetensors").write_bytes(b"\0" * 2048)
    return d


@pytest.fixture
def models_dir(tmp_path, monkeypatch):
    root = tmp_path / "Models"; root.mkdir()
    monkeypatch.setattr(settings, "models_dir", root)
    monkeypatch.setattr(registry, "get_state", lambda k, d=None: None)
    monkeypatch.setattr(registry.runtime, "engine", None)
    return root


def test_lists_mlx_dirs_and_gguf_files_and_hides_drafters(models_dir):
    _mlx(models_dir, "Q-8bit")
    _mlx(models_dir, "Q-MTP-8bit", mtp=True)
    (models_dir / "junk").mkdir()
    (models_dir / "tiny.gguf").write_bytes(b"GGUF" + b"\0" * 100)
    # A hub-layout snapshot counts too, and is named by its repo.
    _mlx(models_dir / "hub" / "models--org--Other-4bit" / "snapshots" / "abc123", "", bits=4) if False else None
    snap = models_dir / "hub" / "models--org--Other-4bit" / "snapshots"
    _mlx(snap, "abc123", bits=4)
    rows = registry.list_local_models()
    names = {r["name"]: r for r in rows}
    assert set(names) == {"Q-8bit", "org/Other-4bit", "tiny.gguf"}     # drafter hidden, junk skipped
    assert names["Q-8bit"]["backend"] == "mlx" and names["Q-8bit"]["drafter"] == "Q-MTP-8bit"
    assert names["Q-8bit"]["mtp"] is True and names["Q-8bit"]["quant"] == "8-bit affine"
    assert names["Q-8bit"]["context_max"] == 8192 and names["Q-8bit"]["fit"]["kv_known"]
    assert names["org/Other-4bit"]["drafter"] is None and names["org/Other-4bit"]["mtp"] is False
    assert names["tiny.gguf"]["backend"] == "gguf"
    assert registry.model_kind(names["Q-8bit"]["path"]) == "mlx"
    assert registry.model_kind(names["tiny.gguf"]["path"]) == "gguf"


def test_engine_settings_carry_both_backends(monkeypatch):
    monkeypatch.setattr(registry, "get_state", lambda k, d=None: None)
    values = registry.engine_settings()
    for key in ("mtp", "ctx_size", "n_slots", "mtp_draft_n", "mlx_server",
                "mlx_decode_concurrency", "mlx_prompt_concurrency", "mlx_prompt_cache_gb",
                "mlx_draft_tokens"):
        assert key in values
    assert values["mlx_server"] == "auto"


def test_drafter_repo_naming_rules():
    assert downloader._is_drafter_repo("mlx-community/Qwen3.8-27B-MTP-8bit")
    assert not downloader._is_drafter_repo("mlx-community/Qwen3.8-27B-8bit")
    assert downloader._is_drafter_repo("someone/Qwen3.8-27B-mtp")
