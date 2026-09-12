"""The engine adapter: the only place in Seymour that knows what an engine or
a model is.

Everything above this file — scheduler, agent, workspace — talks to an
EngineAdapter and never touches an HTTP client, a model name, or an
engine-specific flag. That is not generic good practice; it is a specific bet
about surviving contact with time: backends and models both churn, and a
project that re-litigates those choices never ships (guide, Chapter 3 §3.7).
"""

# ABC machinery for the abstract interface.
from abc import ABC, abstractmethod
# Frozen dataclasses: immutable value objects, safe to share across tasks.
from dataclasses import dataclass, field
# AsyncIterator: stream() yields token deltas as they arrive.
from typing import AsyncIterator, Optional


@dataclass(frozen=True)
class EngineCapabilities:
    """What the running engine can ACTUALLY do — measured, never assumed.

    Produced once by the startup handshake (engine/handshake.py) and passed
    down. The scheduler reads these and never re-derives them from a model
    name. Every field answers one specific silent-failure question.
    """

    total_slots: int              # probe 1: how many slots really exist
    context_per_slot: int         # probe 2: the honest number, post-partition
    kv_unified: bool              # shared KV pool, or statically sliced?
    prompt_caching: bool          # probe 3: measured, not the flag's value
    concurrent: bool              # probe 4: did requests genuinely overlap?
    measured_speedup: float       # probe 4's ratio; ~1.0 means serial
    supports_slot_pinning: bool   # can we control cache affinity?
    supports_tools: bool          # native tool calling (chat-template caps)?
    engine_name: str              # e.g. "llama.cpp b10280" — display only
    model_id: str                 # e.g. the .gguf filename — display only
    supports_vision: bool = False # can the SERVER decode images? True only
                                  # when a vision projector is actually
                                  # loaded (read from /props modalities —
                                  # never inferred from the model's name)
    mtp_enabled: bool = False     # probe 5: is MTP self-speculative decoding
                                  # actually RUNNING? True only when the
                                  # server reported drafted tokens — the
                                  # launch flag alone proves nothing.
    mtp_acceptance: float = 0.0   # probe 5's measured fraction of drafted
                                  # tokens the full model ACCEPTED. This is
                                  # what decides whether MTP pays: accepted
                                  # tokens are nearly free, rejected ones
                                  # cost extra compute. 0.0 = not measured.
    measured_on_battery: bool = False  # were the probes run on battery?
                                  # Measured 2026-09-03: on battery macOS
                                  # duty-cycles GPU compute (bursts of full
                                  # speed, then seconds at ~2 tok/s), so
                                  # every timing above is a duty-cycle
                                  # average and the speedup ratio can land
                                  # anywhere (16x was seen). The UI says
                                  # so instead of showing a clean number.


@dataclass(frozen=True)
class GenerationRequest:
    """One request for text, engine-agnostic.

    The scheduler builds these; adapters translate them to their wire format.
    Note what is ABSENT: no tier, no priority. The engine layer serves
    whatever it is handed — policy lives entirely in the scheduler.
    """

    # The conversation, OpenAI-style: [{"role": "user", "content": "…"}, …].
    messages: list[dict]
    # Hard cap on the reply length, so no request can hold a slot forever.
    max_tokens: int
    # Sampling temperature; 0 for tool-calling turns that must be precise.
    temperature: float = 0.7
    # Optional tool schemas (OpenAI "tools" format) for agent turns.
    tools: Optional[list[dict]] = None
    # Stable per-conversation key. Engines that support cache affinity use it
    # to pin this conversation to one slot so its cached prefix survives;
    # engines that don't simply ignore it. This one field is the whole of the
    # guide's §2.7 fix for the slot-bouncing trap.
    cache_key: Optional[str] = None
    # Extra chat-template arguments (llama.cpp --jinja honors these), e.g.
    # {"enable_thinking": False} to make a reasoning model answer directly.
    # Chat uses it after a web lookup: the model burned its budget thinking
    # about search results and produced no visible answer (a measured bug).
    template_kwargs: Optional[dict] = None
    # Sampling knobs beyond temperature, each None = "the engine's default".
    # They exist because a person tuning a local 35B needs them (Qwen's own
    # card recommends top_p 0.8 / top_k 20 / min_p 0 for non-thinking
    # use, and different numbers for thinking) and because a harness
    # comparison is only fair when both sides sample the same way.
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    min_p: Optional[float] = None
    repeat_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    # Reasoning budget in tokens for thinking models: None = unlimited
    # (the template's default), 0 = no thinking, N = the model is asked to
    # stop thinking after N tokens. Passed through to the engine when it
    # supports the field; the handshake reports whether it does.
    reasoning_budget: Optional[int] = None
    # Per-request telemetry, filled IN by the adapter as the generation runs
    # (the dataclass is frozen but a dict's contents are not — the binding
    # is immutable, the measurements accumulate). After the stream ends the
    # caller can read: prompt_tokens, generated_tokens, decode_tps,
    # prefill_tps, and tps_source ("engine" when the numbers came from the
    # engine's own timers, "measured" when we had to compute them from
    # wall-clock — an Odysseus lesson: never show a number without saying
    # whether it was measured or guessed).
    stats: dict = field(default_factory=dict)


@dataclass
class GenerationStats:
    """Live telemetry an adapter reports about the engine's slots.

    Feeds the scheduler's floor accounting and the status UI. A fake engine
    fills this with made-up-but-consistent numbers; llama.cpp fills it from
    its /slots endpoint.
    """

    slots_total: int = 0          # how many slots exist
    slots_busy: int = 0           # how many are mid-generation right now
    # Per-slot occupancy: list of {"id": 0, "busy": True, "tokens": 1234}.
    slots: list[dict] = field(default_factory=list)


class EngineAdapter(ABC):
    """One serving engine, behind a stable interface."""

    @abstractmethod
    async def start(self) -> None:
        """Bring the engine up (launch a child process, wait until ready)."""

    @abstractmethod
    async def stop(self) -> None:
        """Shut the engine down and release its memory."""

    @abstractmethod
    async def capabilities(self) -> EngineCapabilities:
        """Run the startup handshake and report what is genuinely available."""

    @abstractmethod
    def stream(self, req: GenerationRequest) -> AsyncIterator[str]:
        """Yield token deltas. MUST be cancellable mid-stream — the scheduler
        relies on cancellation for preemption: cancelling the consuming task
        must abort the underlying generation and free the slot."""

    @abstractmethod
    async def complete(self, req: GenerationRequest) -> str:
        """Non-streaming path: return the full reply as one string."""

    @abstractmethod
    async def stats(self) -> GenerationStats:
        """Live telemetry: slots busy/idle, per-slot occupancy."""
