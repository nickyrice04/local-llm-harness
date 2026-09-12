"""A fake engine: deterministic, instant, no model, no network.

This is how the scheduler gets tested at all. Concurrency bugs are timing
bugs, and timing bugs cannot be reproduced against a real 35 GB model that
takes minutes to load and answers differently every run. The fake engine
answers in milliseconds, deterministically, and lets a test control exactly
when each generation finishes.

It also honestly simulates the ONE property the scheduler cares about:
whether generations overlap (concurrent mode) or queue (serial mode).
"""

# asyncio for sleeping and locks — the fake models time, not text quality.
import asyncio
# AsyncIterator matches the adapter's stream() signature.
from typing import AsyncIterator

# The interface this fake must honour, and the records it must produce.
from seymour.engine.adapter import (
    EngineAdapter,
    EngineCapabilities,
    GenerationRequest,
    GenerationStats,
)


class FakeEngine(EngineAdapter):
    """An engine made of sleep() calls.

    Construction flags let a test dial in the behaviour under test:

        FakeEngine(concurrent=False)   → generations serialize (serial mode)
        FakeEngine(delay=0.01)         → each token takes 10 ms
        FakeEngine(tokens=5)           → every reply is 5 tokens long
    """

    def __init__(
        self,
        concurrent: bool = True,   # do generations overlap, or queue?
        total_slots: int = 4,      # how many slots to claim in capabilities
        delay: float = 0.005,      # seconds per emitted token
        tokens: int = 8,           # tokens per reply
    ) -> None:
        self._concurrent = concurrent
        self._total_slots = total_slots
        self._delay = delay
        self._tokens = tokens
        # When NOT concurrent, this lock forces one generation at a time —
        # exactly how a non-batching engine behaves.
        self._serial_lock = asyncio.Lock()
        # Live count of in-flight generations, reported via stats().
        self._busy = 0
        # Every request the fake ever served, for test assertions.
        self.served: list[GenerationRequest] = []

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        """Nothing to launch — the fake is always 'up'."""

    async def stop(self) -> None:
        """Nothing to stop."""

    # ---------------------------------------------------------- capabilities
    async def capabilities(self) -> EngineCapabilities:
        """Report capabilities consistent with the constructor flags."""
        return EngineCapabilities(
            total_slots=self._total_slots if self._concurrent else 1,
            context_per_slot=8192,
            kv_unified=self._concurrent,
            prompt_caching=True,
            concurrent=self._concurrent,
            # A convincing ratio either side of any sane threshold.
            measured_speedup=3.0 if self._concurrent else 1.0,
            supports_slot_pinning=self._concurrent,
            supports_tools=True,
            engine_name="fake",
            model_id="fake-model",
        )

    # ------------------------------------------------------------ generation
    async def _generate(self, req: GenerationRequest) -> AsyncIterator[str]:
        """The shared token producer: N tokens, one every `delay` seconds."""
        self.served.append(req)          # record for test assertions
        self._busy += 1                  # stats() sees this generation
        try:
            for i in range(self._tokens):
                # The sleep is the whole simulation: it yields control so
                # other coroutines run "during" this generation.
                await asyncio.sleep(self._delay)
                yield f"tok{i} "
            # Fill the request's telemetry the way a real engine would —
            # consistent with the constructor's simulated timing.
            req.stats.update({
                "prompt_tokens": 32,
                "generated_tokens": self._tokens,
                "prefill_tps": 1000.0,
                "decode_tps": round(1.0 / self._delay, 1),
                "tps_source": "engine",
            })
        finally:
            # Decrement even when cancelled mid-stream — cancellation is
            # exactly what the scheduler's preemption tests exercise.
            self._busy -= 1

    async def stream(self, req: GenerationRequest) -> AsyncIterator[str]:
        """Yield fake tokens, honouring the concurrency flag."""
        if self._concurrent:
            # Concurrent engine: many generations may interleave freely.
            async for tok in self._generate(req):
                yield tok
        else:
            # Serial engine: the lock makes later requests WAIT, which is
            # the behaviour serial mode's tests need to observe.
            async with self._serial_lock:
                async for tok in self._generate(req):
                    yield tok

    async def complete(self, req: GenerationRequest) -> str:
        """Non-streaming path: just join the stream."""
        parts = [tok async for tok in self.stream(req)]
        return "".join(parts)

    # ---------------------------------------------------------------- stats
    async def stats(self) -> GenerationStats:
        """Report the live in-flight count as busy slots."""
        return GenerationStats(
            slots_total=self._total_slots,
            slots_busy=self._busy,
            slots=[],
        )
