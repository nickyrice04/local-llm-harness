"""The model profile: measured from the engine, stored per model, and an
honest fallback when nothing can be measured."""

import pytest

from seymour.db import init_db
from seymour.engine import profile
from seymour.engine.adapter import GenerationRequest
from seymour.engine.fake import FakeEngine


@pytest.fixture(autouse=True)
def db():
    init_db()


class Thinking(FakeEngine):
    """A fake that reports a reasoning channel and a tokenizer, and
    refuses tools — the three probes, each answered differently."""

    async def _generate(self, req: GenerationRequest):
        if req.tools:
            raise RuntimeError("400: this template does not support tools")
        req.stats["thinking_field"] = "reasoning_content"
        async for tok in super()._generate(req):
            yield tok

    async def tokenize(self, text: str) -> int:
        return 1234


async def test_measure_reads_every_probe_and_stores_the_profile():
    engine = Thinking(delay=0.001)
    caps = await engine.capabilities()
    caps = caps.__class__(**{**caps.__dict__, "model_id": "probe-model"})
    p = await profile.measure(engine, caps, "system prompt " * 100, reuse=False)
    assert p.source == "measured" and p.tools_in_template is False
    assert p.thinking_channel == "reasoning_content" and p.thinking_default_ok
    assert p.system_prompt_tokens == 1234 and p.system_prompt_measured
    assert p.context_tokens == 8192 and p.step_floor(8192) == 2048        # a quarter of the context
    assert "tools in template no" in p.summary() and "measured" in p.summary()
    assert profile.load("probe-model") == p                              # stored per model id
    again = await profile.measure(engine, caps, "x")
    assert again == p                                                     # reused, not re-probed


async def test_the_plain_fake_yields_no_channel_and_an_estimate_and_assumed_is_honest():
    engine = FakeEngine(delay=0.001)
    caps = await engine.capabilities()
    caps = caps.__class__(**{**caps.__dict__, "model_id": "plain-model"})
    p = await profile.measure(engine, caps, "s" * 400, reuse=False)
    assert p.tools_in_template is True and p.thinking_channel is None and not p.thinking_default_ok
    assert p.system_prompt_tokens == 100 and not p.system_prompt_measured   # chars / 4, marked estimated
    fallback = profile.assumed(caps, "s" * 40)
    assert fallback.source == "assumed" and fallback.thinking_channel is None and fallback.tool_protocol == "in-band"
    assert "assumed" in fallback.summary()
