"""The MLX adapter against a fake OpenAI-compatible server (no MLX, no
model): payload mapping, SSE parsing, usage → stats, the in-flight
ledger behind slots(), and the launch command for both servers."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from seymour.engine import mlx as mlx_engine
from seymour.engine.adapter import GenerationRequest


def _sse(chunks: list[dict]) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return body.encode()


def _engine_with(handler) -> mlx_engine.MlxEngine:
    engine = mlx_engine.MlxEngine(model_path=Path("/nonexistent/Q-8bit"))
    engine._client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                       base_url="http://mlx.test")
    engine._total_slots = 3
    engine._ctx = 8192
    return engine


def test_payload_maps_fields_to_the_servers_names():
    engine = _engine_with(lambda r: httpx.Response(200))
    req = GenerationRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=50,
                            temperature=0.2, top_p=0.9, top_k=20, min_p=0.05,
                            repeat_penalty=1.1, presence_penalty=0.3,
                            template_kwargs={"enable_thinking": False}, reasoning_budget=0)
    payload = engine._payload(req, stream=True)
    assert payload["repetition_penalty"] == 1.1 and "repeat_penalty" not in payload
    assert payload["top_k"] == 20 and payload["min_p"] == 0.05 and payload["presence_penalty"] == 0.3
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["stream_options"] == {"include_usage": True}
    assert "reasoning_budget" not in payload            # no wire field on these servers
    assert "stream_options" not in engine._payload(req, stream=False)


async def _run_stream(engine, req):
    out = []
    async for piece in engine.stream(req):
        out.append(piece)
    return "".join(out)


def test_stream_yields_text_and_records_usage_and_ledger():
    seen_busy = []
    engine = None

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        seen_busy.append(len(engine._live))          # NOT live yet: no token has flowed
        chunks = [
            {"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"content": "Hel"}}]},
            {"choices": [{"delta": {"content": "lo"}}], "usage": None},
            {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 2,
                                      "prompt_tokens_details": {"cached_tokens": 8}}},
        ]
        return httpx.Response(200, content=_sse(chunks),
                              headers={"content-type": "text/event-stream"})

    engine = _engine_with(handler)
    req = GenerationRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=5)
    text = asyncio.run(_run_stream(engine, req))
    assert text == "Hello"
    assert seen_busy == [0] and engine._live == {}     # queued ≠ busy; released after
    assert req.stats["prompt_tokens"] == 12 and req.stats["generated_tokens"] == 2
    assert req.stats["cached_tokens"] == 8 and req.stats["tps_source"] == "measured"


def test_complete_returns_content_and_slots_reflect_the_ledger():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1}})
    engine = _engine_with(handler)
    req = GenerationRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=5)
    assert asyncio.run(engine.complete(req)) == "ok"
    assert req.stats["generated_tokens"] == 1
    slots = asyncio.run(engine.slots())
    assert [s["id"] for s in slots] == [0, 1, 2] and all(s["n_ctx"] == 8192 for s in slots)
    assert not any(s["is_processing"] for s in slots)
    engine._live = {1: 0.0, 2: 0.0}
    assert [s["is_processing"] for s in asyncio.run(engine.slots())] == [True, True, False]
    stats = asyncio.run(engine.stats())
    assert stats.slots_total == 3 and stats.slots_busy == 2


def test_http_errors_surface_and_release_the_ledger():
    engine = _engine_with(lambda r: httpx.Response(500, json={"error": "boom"}))
    req = GenerationRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=5)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_run_stream(engine, req))
    assert engine._live == {}


def test_launch_commands_for_both_servers(tmp_path, monkeypatch):
    engine = mlx_engine.MlxEngine(model_path=tmp_path / "Q-8bit")
    monkeypatch.setattr(mlx_engine.settings, "mlx_port", 8099)
    # mlx-lm: batching + prompt cache flags, no drafter.
    engine._server_kind = "mlx-lm"
    cmd = engine._command({"mlx_decode_concurrency": 6, "mlx_prompt_concurrency": 2,
                           "mlx_prompt_cache_gb": 4, "mlx_draft_tokens": 3})
    assert cmd[1:4] == ["-m", "mlx_lm", "server"] and "--decode-concurrency" in cmd
    assert cmd[cmd.index("--decode-concurrency") + 1] == "6"
    assert cmd[cmd.index("--prompt-cache-bytes") + 1] == str(4 * 1024 ** 3)
    assert cmd[cmd.index("--port") + 1] == "8099" and engine._total_slots == 6
    # mlx-vlm with a drafter: MTP flags, one slot.
    drafter = tmp_path / "Q-MTP-8bit"; drafter.mkdir()
    (drafter / "config.json").write_text(json.dumps({"model_type": "qwen3_5_mtp", "block_size": 3,
                                                     "text_config": {"model_type": "qwen3_5_text"}}))
    (drafter / "model.safetensors").write_bytes(b"\0")
    engine._server_kind = "mlx-vlm"; engine._drafter = drafter
    engine._ctx = 65536
    cmd = engine._command({"mlx_draft_tokens": 5, "mlx_decode_concurrency": 3})
    assert cmd[1:4] == ["-m", "mlx_vlm", "server"] and "--draft-kind" in cmd
    assert cmd[cmd.index("--host") + 1] == "127.0.0.1"          # never 0.0.0.0 (its default)
    assert cmd[cmd.index("--max-num-seqs") + 1] == "3" and engine._total_slots == 3
    assert cmd[cmd.index("--max-kv-size") + 1] == "65536"          # enforced by the server
    assert cmd[cmd.index("--draft-block-size") + 1] == "3"   # the drafter's own block size wins
    assert engine._mtp_requested
    # "auto" chooses batching unless MTP is asked for and a drafter exists.
    assert engine._choose_server({"mlx_server": "auto", "mtp": "off"}) == "mlx-lm"
    assert engine._choose_server({"mlx_server": "auto", "mtp": "auto"}) == "mlx-vlm"
    engine._drafter = None
    assert engine._choose_server({"mlx_server": "auto", "mtp": "auto"}) == "mlx-lm"
    assert engine._choose_server({"mlx_server": "mlx-vlm", "mtp": "off"}) == "mlx-vlm"


def test_complete_marks_live_only_once_tokens_flow_and_vlm_thinking_fields():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream"] is True                    # complete() rides the stream
        chunks = [{"choices": [{"delta": {"content": "yes"}}]},
                  {"choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 1},
                   "timings": {"prompt_n": 4, "predicted_n": 1, "prompt_per_second": 400.0,
                               "predicted_per_second": 50.0, "draft_n": 3, "draft_n_accepted": 2}}]
        return httpx.Response(200, content=_sse(chunks), headers={"content-type": "text/event-stream"})
    engine = _engine_with(handler)
    engine._server_kind = "mlx-vlm"
    req = GenerationRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=5,
                            template_kwargs={"enable_thinking": False}, reasoning_budget=512)
    payload = engine._payload(req, stream=True)
    assert payload["enable_thinking"] is False and payload["thinking_budget"] == 512
    assert "chat_template_kwargs" not in payload
    assert asyncio.run(engine.complete(req)) == "yes"
    # Engine timings win over the wall clock, draft counters are per request.
    assert req.stats["tps_source"] == "engine" and req.stats["decode_tps"] == 50.0
    assert req.stats["draft_n"] == 3 and req.stats["draft_n_accepted"] == 2
    assert engine.draft_counters_cumulative is False


def test_vlm_payload_rules_model_seed_and_budget(tmp_path):
    engine = _engine_with(lambda r: httpx.Response(200))
    engine._server_kind = "mlx-vlm"
    engine._model_path = tmp_path / "Q-8bit"
    req = GenerationRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=5,
                            temperature=0.7, repeat_penalty=1.1, reasoning_budget=256)
    engine._drafter = None
    payload = engine._payload(req, stream=False)
    assert payload["model"] == str(tmp_path / "Q-8bit")          # exact loaded path
    assert 1 <= payload["seed"] < 2 ** 31                       # not the server's fixed 0
    assert payload["thinking_budget"] == 256 and payload["repetition_context_size"] == 64
    engine._drafter = tmp_path / "Q-MTP-8bit"
    payload = engine._payload(req, stream=False)
    assert "thinking_budget" not in payload                     # refused with a drafter
    cold = GenerationRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=5, temperature=0.0)
    assert "seed" not in engine._payload(cold, stream=False)     # greedy needs none
    # mlx-lm: a budget of zero becomes thinking off in the template kwargs.
    engine._server_kind = "mlx-lm"
    off = GenerationRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=5, reasoning_budget=0)
    assert engine._payload(off, stream=False)["chat_template_kwargs"] == {"enable_thinking": False}
    assert "model" not in engine._payload(off, stream=False)


def test_in_band_error_chunk_and_closed_connection_are_named():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse([{"error": "Model type qwen3_5_mtp not supported"}]),
                              headers={"content-type": "text/event-stream"})
    engine = _engine_with(handler)
    req = GenerationRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=5)
    with pytest.raises(RuntimeError, match="qwen3_5_mtp"):
        asyncio.run(_run_stream(engine, req))
    assert engine._live == {}

    def closes(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
    engine = _engine_with(closes)
    with pytest.raises(RuntimeError, match="without a reply"):
        asyncio.run(_run_stream(engine, req))
