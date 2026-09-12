"""The model profile: what THIS model can do, measured at load — so a new
model runs the day it comes out, degraded and honest when it must.

The handshake (handshake.py) measures the ENGINE: slots, context,
caching, overlap. This measures the MODEL behind it, the things the
harness used to assume from Qwen's habits:

    tools_in_template   does the chat template accept a `tools` list?
                        (a request with one trivial tool spec either
                        works or the server refuses it)
    thinking_channel    does the model have a hidden reasoning channel,
                        and which field carries it on the wire
                        (`reasoning_content` on llama-server / mlx-lm),
                        or none — in which case thinking knobs are noise
    system_prompt_tokens the REAL token cost of Seymour's system prompt
                        under this model's tokenizer (/tokenize), or an
                        estimate marked as one when the engine has no
                        tokenizer endpoint (the MLX servers)
    context_tokens      copied from the engine handshake, for the floors

The profile is stored per model id (app_state) so a reload does not
re-probe, and it drives three defaults: the tool protocol stays in-band
(native calling is an eval-gated experiment — NOTES.md 2026-09-11), the
thinking default is OFF when there is no channel to think in, and the
per-step token floor never exceeds a quarter of the context. An unknown
model without measurements still runs: `assumed()` is the fallback and
says so in every place the profile is shown.
"""

import json
import logging
import time
from dataclasses import asdict, dataclass

from seymour.db import get_state, set_state
from seymour.engine.adapter import EngineCapabilities, GenerationRequest

logger = logging.getLogger(__name__)

STATE_PREFIX = "model_profile:"
# A trivial tool spec for the template probe — the shape is what is tested.
_PROBE_TOOL = [{"type": "function", "function": {"name": "ping", "description": "ping",
                                                  "parameters": {"type": "object", "properties": {}}}}]


@dataclass(frozen=True)
class ModelProfile:
    model_id: str
    context_tokens: int
    vision: bool
    tools_in_template: bool
    thinking_channel: str | None          # "reasoning_content" | None
    system_prompt_tokens: int
    system_prompt_measured: bool          # tokenizer-counted, or chars/4
    source: str                           # "measured" | "assumed"
    measured_at: float = 0.0

    # ---- the defaults the profile decides ---------------------------------
    @property
    def tool_protocol(self) -> str:
        """In-band JSON everywhere until native calling wins an eval
        (NOTES.md, 2026-09-11) — the field records what WOULD be possible."""
        return "in-band"

    @property
    def thinking_default_ok(self) -> bool:
        """May a run kind turn thinking on? Only with a channel to think in."""
        return self.thinking_channel is not None

    def step_floor(self, wanted: int = 8192) -> int:
        """The per-step reply floor: the wanted tokens, never more than a
        quarter of the context (a 16k model cannot spend 8k on one step)."""
        return max(1024, min(wanted, self.context_tokens // 4))

    def summary(self) -> str:
        return (f"profile {self.source}: tools in template {'yes' if self.tools_in_template else 'no'} · "
                f"thinking channel {self.thinking_channel or 'none'} · system prompt "
                f"{self.system_prompt_tokens:,} tokens ({'measured' if self.system_prompt_measured else 'estimated'}) · "
                f"context {self.context_tokens:,}")


def assumed(caps: EngineCapabilities, system_prompt: str) -> ModelProfile:
    """The fallback for a model nothing could be measured on: runs, degraded
    (no thinking, in-band tools, floors from the context), and says so."""
    return ModelProfile(model_id=caps.model_id, context_tokens=caps.context_per_slot, vision=caps.supports_vision,
                        tools_in_template=bool(caps.supports_tools), thinking_channel=None,
                        system_prompt_tokens=len(system_prompt) // 4, system_prompt_measured=False,
                        source="assumed", measured_at=time.time())


def load(model_id: str) -> ModelProfile | None:
    raw = get_state(STATE_PREFIX + model_id)
    if not raw:
        return None
    try:
        return ModelProfile(**json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        return None


def save(profile: ModelProfile) -> None:
    set_state(STATE_PREFIX + profile.model_id, json.dumps(asdict(profile)))


async def measure(engine, caps: EngineCapabilities, system_prompt: str, reuse: bool = True) -> ModelProfile:
    """Probe the model behind `engine`; store and return its profile. Never
    raises: a probe that fails leaves its field at the honest default and
    the profile is still 'measured' for the rest."""
    if reuse:
        cached = load(caps.model_id)
        if cached is not None:
            return cached
    # ---- tools in the template ---------------------------------------------
    tools_ok = False
    try:
        request = GenerationRequest(messages=[{"role": "user", "content": "Reply with the single word: ready."}],
                                    max_tokens=4, temperature=0.0, tools=_PROBE_TOOL,
                                    template_kwargs={"enable_thinking": False})
        await engine.complete(request)
        tools_ok = True
    except Exception as error:
        logger.info("profile: template refuses tools (%s)", str(error)[:120])
    # ---- the thinking channel ------------------------------------------------
    channel = None
    try:
        request = GenerationRequest(messages=[{"role": "user", "content": "What is 2+2? Answer briefly."}],
                                    max_tokens=48, temperature=0.0)
        async for _ in engine.stream(request):
            pass
        channel = request.stats.get("thinking_field")
    except Exception as error:
        logger.info("profile: thinking probe failed (%s)", str(error)[:120])
    # ---- the system prompt's real cost -------------------------------------
    tokens, measured = len(system_prompt) // 4, False
    tokenize = getattr(engine, "tokenize", None)
    if tokenize is not None:
        try:
            count = await tokenize(system_prompt)
            if count:
                tokens, measured = int(count), True
        except Exception as error:
            logger.info("profile: tokenize failed (%s)", str(error)[:120])
    profile = ModelProfile(model_id=caps.model_id, context_tokens=caps.context_per_slot, vision=caps.supports_vision,
                           tools_in_template=tools_ok, thinking_channel=channel,
                           system_prompt_tokens=tokens, system_prompt_measured=measured,
                           source="measured", measured_at=time.time())
    save(profile)
    logger.info("profile: %s", profile.summary())
    return profile
