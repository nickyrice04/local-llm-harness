"""The startup handshake: measure what the engine can ACTUALLY do.

Flags are requests, not facts. `--parallel 4` asks for four slots; the
build, the model, or memory limits may have given fewer. `--kv-unified` asks
for a shared KV pool; if it silently didn't take, each slot holds ctx/N and
your agent can't fit its task. `--cache-prompt` may be on and useless for
this model. Concurrent requests may be quietly queueing. None of these fail
loudly — the server starts, inference works, output is correct — and the
property the whole application is built on may be absent.

So Seymour asks the running process. Four probes, four silent failures:

    probe 1  how many slots really exist          (/props total_slots)
    probe 2  the honest context per slot          (/slots n_ctx — catches
                                                   static partitioning)
    probe 3  does prompt caching measurably work  (repeat-prompt timing)
    probe 4  do requests genuinely overlap        (measured speedup ratio)

The rule the whole project keeps returning to: never infer a capability
from a model card, a documentation page, or a support matrix. Measure it
from the running engine.
"""

# asyncio for concurrent probe requests; time for the stopwatch.
import asyncio
import logging
import time

# The measurements land in this frozen record.
from seymour.engine.adapter import EngineCapabilities, GenerationRequest
# Thresholds come from configuration.
from seymour.config import settings
# The bus announces each probe's verdict to the UI.
from seymour.events import bus

logger = logging.getLogger(__name__)

# A deliberately long, boring prompt (~700 tokens once repeated): probe 3
# times PREFILL, so the prompt must be long enough that prefill dominates
# the measurement. Content is irrelevant; length is the point.
_LONG_PROMPT = (
    "You are helping test a local inference server. Read the following "
    "paragraph and then reply with the single word: ready. "
    + ("The quick brown fox jumps over the lazy dog near the riverbank while "
       "the afternoon light settles across the valley and the observers take "
       "careful notes about everything they see. ") * 40
)


async def _timed_completion(engine, prompt: str, cache_key=None, max_tokens: int = 1) -> float:
    """Run one tiny completion and return its wall-clock seconds.

    max_tokens=1 makes the elapsed time ≈ prefill time (time-to-first-token),
    which is exactly what the caching probe needs to compare.
    """
    req = GenerationRequest(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=0.0,          # deterministic — this is a measurement
        cache_key=cache_key,      # present = exercise the affinity path too
    )
    start = time.perf_counter()
    await engine.complete(req)
    return time.perf_counter() - start


async def run_handshake(engine) -> EngineCapabilities:
    """Run all four probes against a ready engine; return frozen capabilities."""
    logger.info("handshake: starting (4 probes)")
    bus.publish("engine", "handshake_started")
    # Power source first: on battery macOS duty-cycles GPU compute, and
    # every number below becomes an average over fast and crawling
    # phases (measured 2026-09-03: 17 tok/s bursts, then 10-30 s at
    # ~2 tok/s, GPU idle while work is queued). The probes still run —
    # the verdicts (caching works, requests overlap) survive it — but the
    # record says how they were taken, and the UI repeats it.
    from seymour.agent import power
    on_battery = bool(power.on_battery())
    if on_battery:
        logger.warning("handshake: running ON BATTERY — GPU compute is "
                       "duty-cycled; timings are bursty and the speedup "
                       "ratio is not meaningful. Re-measure on AC.")

    # ---- Probes 1 & 2: configuration, read back from the server ----------
    # /props reports what the server actually built (not what we asked for);
    # /slots reports each slot's real context window.
    props = await engine.props()
    slot_list = await engine.slots()

    # Probe 1: the honest slot count.
    total_slots = int(props.get("total_slots") or len(slot_list) or 1)

    # Probe 2: the honest per-slot context. Under a unified KV pool every
    # slot reports (about) the full --ctx-size; under static partitioning
    # each reports ctx/N — the trap where adding slots silently divides
    # your context window.
    slot_ctxs = [int(s.get("n_ctx") or 0) for s in slot_list] or [settings.ctx_size]
    context_per_slot = min(c for c in slot_ctxs if c > 0)
    kv_unified = context_per_slot >= int(settings.ctx_size * 0.9)
    if not kv_unified:
        logger.warning(
            "handshake: static KV partitioning detected — each slot has only "
            "%d tokens of the %d requested", context_per_slot, settings.ctx_size,
        )

    # Native tool calling: read from the chat template's capabilities, and
    # measured behaviour wins over absence of the field (older builds may
    # not report it; assume True and let the agent's fallback handle it).
    template_caps = props.get("chat_template_caps") or {}
    supports_tools = bool(
        template_caps.get("supports_tools", template_caps.get("supports_tool_calls", True))
    )

    # Vision: TRUE only when the server says a projector is loaded (the
    # /props "modalities" block). The model's NAME saying -VL proves
    # nothing — without the mmproj file next to the weights, images would
    # be silently ignored. Absence of the field = no vision (fail closed:
    # a false "no" degrades to text; a false "yes" silently drops images).
    modalities = props.get("modalities") or {}
    supports_vision = bool(modalities.get("vision", False))

    # ---- Probe 3: does prompt caching measurably work? --------------------
    # Send a long prompt (cold prefill), then the same prompt again on the
    # same cache_key. A real cache hit is dramatic and unmistakable: the
    # second prefill should collapse to (nearly) nothing.
    cold = await _timed_completion(engine, _LONG_PROMPT, cache_key="handshake-probe")
    warm = await _timed_completion(engine, _LONG_PROMPT, cache_key="handshake-probe")
    # "Under half the cold time" is a deliberately loose line: a true hit is
    # usually 5-20x faster, and a miss is ~1x. Nothing sits near 0.5.
    prompt_caching = warm < cold * 0.5
    logger.info("handshake: caching probe cold=%.2fs warm=%.2fs → %s",
                cold, warm, "works" if prompt_caching else "NOT working")

    # ---- Probe 4: do concurrent requests genuinely overlap? ---------------
    # One solo request, timed; then three DISTINCT prompts (distinct so the
    # cache cannot flatter the result) fired simultaneously, timed together.
    # If the server truly batches, the concurrent wall time barely exceeds
    # the solo time; if it queues, it's ~3x. speedup = 3*solo / concurrent.
    n = min(3, total_slots)
    if n >= 2:
        # WARM UP FIRST, and properly. A just-loaded model page-faults its
        # weights in on the first real work, which serializes everything
        # and makes a good machine look serial. This matters double on an
        # MoE: with hundreds of experts, only the few each token routes
        # through get faulted in, so a two-word warmup leaves most of the
        # model cold. Varied prompts and real length touch far more of it.
        # Measured on the M5 Max: cold probed 1.04x, then 1.17x with a
        # token warmup, and 1.76x genuinely warm — same server, same flags.
        await asyncio.gather(*(
            _timed_completion(
                engine,
                f"Warm up {i}: name {i + 4} unrelated countries, then "
                f"count from 1 to 15, then list {i + 3} colors.",
                max_tokens=160,
            )
            for i in range(n)
        ))

        async def _peak_busy(stop: asyncio.Event) -> int:
            """Poll /slots while a burst runs; return the most slots seen
            processing at the same instant. This is GROUND TRUTH for the
            question that actually matters: does this engine run
            sequences simultaneously?"""
            peak = 0
            while not stop.is_set():
                try:
                    live = await engine.slots()
                    peak = max(peak, sum(1 for slot in live
                                         if slot.get("is_processing")))
                except Exception:
                    pass              # introspection is a bonus, not a need
                await asyncio.sleep(0.2)
            return peak

        async def _attempt() -> tuple[float, int]:
            """One solo-vs-concurrent comparison → (speedup, peak busy)."""
            solo = await _timed_completion(
                engine, "Solo probe. Count from 1 to 20, digits only.",
                max_tokens=64,
            )
            stop = asyncio.Event()
            watcher = asyncio.create_task(_peak_busy(stop))
            start = time.perf_counter()
            await asyncio.gather(*(
                _timed_completion(
                    # Each prompt is unique — a shared prefix would let
                    # the prompt cache do the work and we would measure
                    # the cache, not the batching.
                    engine,
                    f"Concurrent probe number {i}. List {i + 3} animals.",
                    max_tokens=64,
                )
                for i in range(n)
            ))
            wall = time.perf_counter() - start
            stop.set()
            peak = await watcher
            return ((n * solo) / wall if wall > 0 else 1.0), peak

        # BEST OF SEVERAL, and that is not cherry-picking: every source of
        # error here is one-directional. Cold weights, a background task,
        # another process on the GPU — all DEPRESS these numbers; nothing
        # inflates them. The best attempt is the closest to the truth.
        measured_speedup = 0.0
        peak_busy = 0
        for attempt in range(3):
            speedup, peak = await _attempt()
            measured_speedup = max(measured_speedup, speedup)
            peak_busy = max(peak_busy, peak)
            if peak_busy >= 2 and measured_speedup >= settings.concurrency_threshold:
                break                 # convincingly concurrent; stop early
            logger.info("handshake: overlap attempt %d gave %.2fx, peak %d "
                        "slots busy — re-probing", attempt + 1, speedup, peak)
    else:
        # A single slot cannot overlap anything; skip the probe honestly.
        measured_speedup = 1.0
        peak_busy = 0
    # THE VERDICT — and what it is really asking. The old test was pure
    # throughput ("is 3-at-once at least 1.5x faster than 3-in-a-row?"),
    # which quietly answers the wrong question. What concurrency buys
    # Seymour is OVERLAP: a chat reply that starts now instead of waiting
    # behind the agent's long generation. That is latency isolation, and
    # it is worth having even when aggregate throughput barely moves.
    #
    # Measured case that forced this: with MTP drafting on, three slots
    # genuinely ran at once (peak_busy = 3) while each stream slowed from
    # ~90 to ~42 tok/s — a real 1.35x aggregate gain that the 1.5
    # threshold rejected, collapsing a 4-slot engine to ONE slot and
    # taking the primary agent's whole reason to exist with it.
    #
    # So: if the engine demonstrably runs sequences simultaneously, it is
    # concurrent. The throughput ratio remains as the fallback for
    # adapters that can't introspect their slots.
    if peak_busy >= 2:
        concurrent = True
    else:
        concurrent = measured_speedup >= settings.concurrency_threshold
    logger.info("handshake: overlap probe n=%d speedup=%.2fx peak_busy=%d → %s",
                n, measured_speedup, peak_busy,
                "concurrent" if concurrent else "serial")

    # ---- Probe 5: is MTP self-speculative decoding actually working? ------
    # The launch flag proves nothing on its own (and an ADOPTED server's
    # flags are unknown to us entirely), so ask the work itself: generate
    # a short STRUCTURED completion — the shape where drafting pays, and
    # the shape Seymour generates constantly in tool calls — and read the
    # server's own draft counters. Acceptance is the number that decides
    # whether MTP earns its keep: accepted tokens are nearly free,
    # rejected ones cost extra compute.
    mtp_enabled, mtp_acceptance = False, 0.0
    try:
        # The counters are CUMULATIVE server-wide, so two probes and a
        # subtraction give this generation's own numbers.
        warmup = GenerationRequest(
            messages=[{"role": "user", "content": "Say ok."}],
            max_tokens=4, temperature=0.0)
        await engine.complete(warmup)
        before_n = warmup.stats.get("draft_n") or 0
        before_ok = warmup.stats.get("draft_n_accepted") or 0
        request = GenerationRequest(
            messages=[{"role": "user", "content":
                       "List the numbers 1 through 24, one per line, "
                       "formatted exactly as 'Item N'."}],
            max_tokens=96,
            temperature=0.0,
        )
        await engine.complete(request)
        if getattr(engine, "draft_counters_cumulative", True):
            drafted = (request.stats.get("draft_n") or 0) - before_n
            accepted = (request.stats.get("draft_n_accepted") or 0) - before_ok
        else:
            # The MLX servers report each request's own counters — no
            # subtraction, or the warmup's numbers would be taken away.
            drafted = request.stats.get("draft_n") or 0
            accepted = request.stats.get("draft_n_accepted") or 0
        if drafted > 0:
            mtp_enabled = True
            mtp_acceptance = accepted / drafted
        logger.info("handshake: MTP probe drafted=%d accepted=%d → %s",
                    drafted, accepted,
                    f"{mtp_acceptance:.0%} accepted" if mtp_enabled
                    else "not drafting (MTP off or unsupported)")
    except Exception:
        logger.debug("MTP probe skipped", exc_info=True)

    # ---- Freeze the result ------------------------------------------------
    caps = EngineCapabilities(
        total_slots=total_slots,
        context_per_slot=context_per_slot,
        kv_unified=kv_unified,
        prompt_caching=prompt_caching,
        concurrent=concurrent,
        measured_speedup=round(measured_speedup, 2),
        # complete() flips this off if the build rejected id_slot mid-probe.
        supports_slot_pinning=engine.slot_pinning_ok,
        supports_tools=supports_tools,
        supports_vision=supports_vision,
        mtp_enabled=mtp_enabled,
        mtp_acceptance=round(mtp_acceptance, 3),
        measured_on_battery=on_battery,
        # An engine that knows its own label (the MLX adapter: server +
        # version + drafter) says so; llama-server is named by its build.
        engine_name=(getattr(engine, "engine_label", None)
                     or f"llama.cpp (build {props.get('build_info', '?')})"),
        model_id=getattr(engine, "_model_path", None) and engine._model_path.name or "unknown",
    )
    # Tell the UI everything the probes found — this feeds the mode banner.
    bus.publish("engine", "handshake_done", caps=caps.__dict__)
    logger.info("handshake: %s", caps)
    return caps
