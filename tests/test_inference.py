"""Inference settings: clamped, resolvable, budgeted, and honest in the
trace. Model-free."""

from seymour import inference
from seymour.db import init_db


def setup_module():
    init_db()


def test_defaults_follow_the_model_card():
    inference.save(inference.defaults())
    cur = inference.current()
    assert (cur.temperature, cur.top_p, cur.top_k, cur.min_p) == (0.7, 0.8, 20, 0.0)
    assert cur.thinking == "auto"


def test_values_are_clamped_and_garbage_falls_back():
    cur = inference.save({"temperature": 70, "top_k": -5, "thinking": "sometimes",
                          "max_tokens": "lots"})
    assert cur.temperature == 2.0 and cur.top_k == 0
    assert cur.thinking == "auto" and cur.max_tokens == 8192
    inference.save(inference.defaults())


def test_overrides_apply_to_one_call_only():
    base = inference.current()
    over = inference.current({"thinking": "on", "temperature": 0.1})
    assert over.thinking == "on" and over.temperature == 0.1
    assert inference.current().thinking == base.thinking       # nothing persisted


def test_thinking_resolution_and_request_kwargs():
    inference.save({"thinking": "auto", "reasoning_budget": 512})
    cur = inference.current()
    assert cur.enable_thinking(False) is False and cur.enable_thinking(True) is True
    off = cur.request_kwargs(thinking_default=False)
    assert off["template_kwargs"] == {"enable_thinking": False} and "reasoning_budget" not in off
    on = cur.request_kwargs(thinking_default=True)
    assert on["template_kwargs"] is None and on["reasoning_budget"] == 512
    assert inference.current({"thinking": "off"}).enable_thinking(True) is False
    inference.save(inference.defaults())


def test_history_budget_drops_oldest_keeps_latest():
    history = [{"role": "user", "content": "x" * 4000}] * 5 + [{"role": "user", "content": "latest"}]
    kept = inference.budget_history(history, limit_tokens=2100)   # ~8400 chars
    assert kept[-1]["content"] == "latest" and len(kept) == 3
    assert inference.budget_history(history, limit_tokens=1)[-1]["content"] == "latest"


def test_trace_names_the_effective_thinking():
    inference.save({"thinking": "on"})
    assert inference.current().trace(thinking_default=False)["thinking_effective"] is True
    inference.save(inference.defaults())
