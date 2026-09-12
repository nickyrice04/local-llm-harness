"""The real engine: launch, supervise, and talk to llama-server.

This is the ONLY module that knows llama-server exists. Everything above it
talks to the EngineAdapter interface. Three responsibilities:

    1. Own llama-server as a child process — started at boot, killed at exit,
       with readiness polled (never assumed) and output captured to a log.
    2. Translate GenerationRequests to the OpenAI-compatible wire format,
       and SSE frames back into bare text deltas.
    3. Manage slot affinity: keep each conversation on the same slot so its
       cached prefix survives between turns (llama.cpp's --cache-prompt
       reuses a prefix only within a slot — "only the unseen suffix is
       evaluated" — and default slot assignment can bounce a conversation
       between slots, silently re-prefilling everything. Odysseus hit and
       fixed exactly this; see ACKNOWLEDGMENTS.md).
"""

# asyncio for the readiness poll; json for SSE frames; logging for the trail.
import asyncio
import json
import logging
# subprocess launches llama-server as a child process we own.
import subprocess
import signal
import os
# Path for the model file (and for reading /props' model_path back).
from pathlib import Path
# AsyncIterator: stream() yields token deltas.
from typing import AsyncIterator, Optional

# httpx is our async HTTP client to the server.
import httpx

# Configuration: paths, ports, and every launch flag's value.
from seymour.config import settings
# The interface we implement and the records we produce.
from seymour.engine.adapter import (
    EngineAdapter,
    EngineCapabilities,
    GenerationRequest,
    GenerationStats,
)
# The bus tells the UI about engine lifecycle (loading / ready / down).
from seymour.engine.gguf import read_info
from seymour.engine.guard import guarded
from seymour.events import bus

logger = logging.getLogger(__name__)


class LlamaCppEngine(EngineAdapter):
    """A running llama-server, owned by this process."""

    def __init__(self, model_path=None) -> None:
        # The .gguf to serve; defaults to the configured model. The model
        # manager passes a different path when the user switches models.
        self._model_path = model_path or settings.model_path
        # The child process handle (None until start()).
        self._process: Optional[subprocess.Popen] = None
        # One shared async HTTP client, created at start().
        self._client: Optional[httpx.AsyncClient] = None
        # Slot affinity: cache_key → slot id. A conversation keeps its slot
        # for its whole life, so its cached prefix stays warm.
        self._slot_of: dict[str, int] = {}
        # Round-robin cursor for assigning fresh cache_keys to slots.
        self._next_slot = 0
        # Which conversation is LIVE on each slot right now (slot →
        # (cache_key, in-flight request count)). Two different
        # conversations must never be pinned to one slot at the same time:
        # measured 2026-09-02, an agent task and a chat run that shared a
        # slot came back with each other's text — the agent's file listing
        # spliced into the chat's tool call, the chat's URL in the agent's
        # journal. Blind round-robin over 4 slots with dozens of
        # conversations made that collision routine; this ledger makes it
        # impossible (see _slot_for).
        self._live: dict[int, tuple[str, int]] = {}
        # Which conversation each slot served LAST (slot → cache_key). A
        # request may reuse a slot's cached prefix ONLY when the slot's
        # last occupant was the same conversation. Measured 2026-09-02:
        # when a slot that held one conversation's cache received another
        # conversation's prompt with cache_prompt on, llama-server's
        # hybrid-model path (Qwen3.6 has recurrent layers, so cache reuse
        # means restoring a recurrent-state checkpoint) wedged the slot —
        # "prompt processing … 0.79 tokens per second" while erasing and
        # recreating a 63 MiB checkpoint every 17 ms, for minutes. A cold
        # prefill of a 6k prompt costs ~3 s; a wedged slot costs every
        # request pinned to it. So: same conversation → warm cache;
        # anything else → cache_prompt off, fresh prefill.
        self._last_key: dict[int, str] = {}
        # Slots the SERVER says are processing (from /slots, refreshed by
        # stats()). Our own ledger cannot see a request whose client gave
        # up while the server kept going — a ghost — and pinning a new
        # request onto a ghost's slot queues it behind the ghost forever.
        self._server_busy: set[int] = set()
        # How many slots exist — learned from /props after startup.
        self._total_slots = settings.n_slots
        # Set False if the running build rejects the id_slot field; the
        # handshake reads it into capabilities.supports_slot_pinning.
        self.slot_pinning_ok = True
        # Filled by the handshake and cached here (capabilities() returns it).
        self._caps: Optional[EngineCapabilities] = None
        # True when we ADOPTED a llama-server someone else started (see
        # start()). An adopted server is never ours to kill.
        self._external = False
        # True when WE launched this server with MTP self-speculative
        # decoding enabled. An adopted server's flags are unknown to us,
        # so this stays False there — the handshake reports what it can
        # measure, never what it hoped for.
        self._mtp_requested = False

    # ------------------------------------------------------------- lifecycle
    async def start(self, adopt: str = "always") -> None:
        """Launch llama-server and block until it answers requests.

        One special case first: if something ALREADY answers on our port —
        say, a llama-server left running from a manual session — launching
        our own would collide on the bind and load a second 35 GB copy.
        The `adopt` policy says what to do about it:

            "always"     — BOOT: adopt whatever is running, measure it
                           (the handshake never trusted flags anyway),
                           report it as external, never kill it.
            "same-model" — explicit LOAD: adopt ONLY if it serves the very
                           model the user asked for (then loading is
                           instant and correct); a different model is a
                           clear refusal naming the remedy. This is what
                           makes Load work when your own manual server is
                           already serving the same weights.
            "never"      — refuse any occupied port outright.
        """
        self._client = httpx.AsyncClient(base_url=settings.llama_url, timeout=300.0)
        try:
            health = await self._client.get("/health", timeout=1.0)
        except httpx.RequestError:
            health = None                     # nothing there — normal path
        if health is not None and health.status_code == 200 and adopt != "always":
            # Someone else's server holds the port. Whose model is it?
            served = "unknown model"
            try:
                served = Path((await self.props()).get("model_path", served)).name
            except httpx.HTTPError:
                pass
            # The one acceptable case: it serves EXACTLY what the user
            # asked to load — adopting it IS loading it (and instant).
            if adopt == "same-model" and served == self._model_path.name:
                logger.info("the running server already serves %s — adopting it",
                            served)
            else:
                await self._client.aclose()
                raise RuntimeError(
                    f"port {settings.llama_port} is already serving {served} — "
                    f"a llama-server Seymour did not start. Stop that server "
                    f"(or change SEYMOUR_LLAMA_PORT) and try again."
                )
        if health is not None and health.status_code == 200:
            self._external = True
            try:
                props = await self.props()
                # The truth about what's being served comes from the
                # server, not from our configuration.
                served = props.get("model_path")
                if served:
                    self._model_path = Path(served)
                self._total_slots = int(props.get("total_slots") or settings.n_slots)
            except httpx.HTTPError:
                pass                          # handshake will probe again
            logger.warning(
                "adopting an already-running llama-server on %s (serving %s) — "
                "Seymour did not launch it and will not stop it",
                settings.llama_url, self._model_path.name,
            )
            bus.publish("engine", "ready", model=self._model_path.name,
                        external=True)
            return

        # Fail early with a clear message if the weights aren't there.
        if not self._model_path.exists():
            raise FileNotFoundError(
                f"Model not found: {self._model_path}\n"
                f"Set SEYMOUR_MODEL_PATH in .env, or download a model in the "
                f"Models tab."
            )

        # What the user chose in the Models tab (falling back to the
        # config defaults). Imported here, not at module scope: the
        # registry imports this module, so a top-level import would cycle.
        from seymour.models_manager.registry import engine_settings
        chosen = engine_settings()

        # The launch command. Every flag we RELY on is passed explicitly,
        # never inherited from defaults that can change between releases:
        cmd = [
            "llama-server",
            "-m", str(self._model_path),          # the weights
            "--host", settings.llama_host,        # loopback ONLY (security)
            "--port", str(settings.llama_port),
            "--parallel", str(chosen["n_slots"]),  # N slots = N concurrent
                                                  # sequences (the scheduler
                                                  # divides them among tiers)
            "--ctx-size", str(chosen["ctx_size"]), # TOTAL context budget
            "--kv-unified",                       # one shared KV pool. Its
                                                  # default is on ONLY when
                                                  # slot count is auto — we
                                                  # set --parallel, so we
                                                  # must ask explicitly or
                                                  # get ctx/N per slot.
            "--cache-prompt",                     # prompt caching via slot
                                                  # continuation (documented
                                                  # default, passed anyway)
            "--cache-type-k", settings.cache_type_k,  # 8-bit KV keys. NOT q4:
                                                  # it desynchronizes hybrid
                                                  # models' recurrent state.
            "--jinja",                            # use the model's own chat
                                                  # template, which is what
                                                  # enables native tool calls
            "--metrics",                          # expose /metrics
        ]
        # MTP (Multi-Token Prediction): if THESE weights carry MTP heads,
        # turn on self-speculative decoding. The head drafts the next few
        # tokens and the full model verifies them in one pass, so accepted
        # tokens are nearly free. Read from the file's own header — a
        # model without the heads must never get the flag, or the server
        # refuses to start. The handshake measures whether it actually
        # helped; this only enables it.
        if chosen["mtp"] != "off":
            info = read_info(self._model_path)
            if info.supports_mtp:
                cmd += ["--spec-type", "draft-mtp",
                        "--spec-draft-n-max", str(chosen["mtp_draft_n"])]
                self._mtp_requested = True
                logger.info("MTP heads found (%d nextn layer(s)) — enabling "
                            "self-speculative decoding, drafting %d",
                            info.nextn_layers, chosen["mtp_draft_n"])

        # Vision: if a projector file (mmproj*.gguf) sits next to the
        # weights, load it — that is what makes the model's vision REAL
        # (the handshake then reads it back from /props; nothing here is
        # trusted, only enabled). Downloaded vision models ship it as a
        # sibling file in the same repo directory.
        mmproj = sorted(self._model_path.parent.glob("*mmproj*.gguf"))
        if mmproj:
            cmd += ["--mmproj", str(mmproj[0])]
            logger.info("vision projector found: %s", mmproj[0].name)
        logger.info("starting llama-server: %s", " ".join(cmd))
        bus.publish("engine", "loading", model=self._model_path.name)

        # llama-server is extremely chatty; capture its output to a log file
        # so a failed startup can be diagnosed without drowning our own logs.
        log_dir = settings.data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._log_file = open(log_dir / "llama-server.log", "ab")
        # Under the child guard (engine/guard.py) and in its own process
        # group: a Seymour that dies without stop() — killed hard, crashed
        # — no longer orphans a 35 GB llama-server (measured 2026-09-02).
        self._process = subprocess.Popen(
            guarded(cmd), stdout=self._log_file, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        # (The HTTP client already exists — start() created it for the
        # adoption probe. Its 300 s timeout is deliberate: a 35 GB model's
        # first token after a long prompt can genuinely take a while.)
        await self._wait_until_ready()
        # Learn the real slot count now — /props reports what the server
        # actually built, which is what affinity assignment must use.
        try:
            props = await self.props()
            self._total_slots = int(props.get("total_slots") or settings.n_slots)
        except httpx.HTTPError:
            pass                      # handshake will probe again anyway
        logger.info("llama-server ready on %s", settings.llama_url)
        bus.publish("engine", "ready", model=self._model_path.name)

    async def _wait_until_ready(self, timeout_s: float = 600.0) -> None:
        """Poll /health until the model is loaded.

        We poll rather than sleep-and-hope because load time varies
        enormously between a cold and a warm start (page cache). And we
        check the child is still alive on every pass: a server that died
        because the model didn't fit becomes an immediate, clear error
        instead of a ten-minute silent timeout.
        """
        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            # The dead-child check — the most important line in this loop.
            if self._process and self._process.poll() is not None:
                raise RuntimeError(
                    f"llama-server exited during startup "
                    f"(code {self._process.returncode}). Out of memory? "
                    f"See {settings.data_dir}/logs/llama-server.log"
                )
            try:
                response = await self._client.get("/health", timeout=2.0)
                if response.status_code == 200:
                    return
            except httpx.RequestError:
                pass                  # not listening yet — expected during load
            await asyncio.sleep(1.0)
        raise TimeoutError(f"llama-server not ready after {timeout_s}s")

    @property
    def external(self) -> bool:
        """True when this engine ADOPTED a server someone else started —
        one we measure and use, but must never kill or replace."""
        return self._external

    @property
    def model_name(self) -> str:
        """The served model's filename (for display and comparisons)."""
        return self._model_path.name

    @property
    def model_path(self):
        """The weights file this engine serves (a Path) — public so the
        vision gate can check for an mmproj sibling when explaining WHY
        images can't be seen."""
        return self._model_path

    async def stop(self) -> None:
        """Shut the server down cleanly. Called on app exit — always.

        Without this, quitting Seymour would orphan a 35 GB child process,
        and the next run would try to load a second copy. An ADOPTED
        server is not ours to kill: stopping just detaches from it.
        """
        if self._external:
            logger.info("detaching from external llama-server (left running)")
        if self._client:
            await self._client.aclose()
        if self._process and self._process.poll() is None:
            # The whole group: the guard forwards SIGTERM to the server
            # and mirrors its exit; SIGKILL to the group is the insist.
            try:
                os.killpg(self._process.pid, signal.SIGTERM)   # ask nicely
            except ProcessLookupError:
                pass
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self._process.pid, signal.SIGKILL)  # insist
                except ProcessLookupError:
                    pass
            logger.info("llama-server stopped")
        if getattr(self, "_log_file", None):
            self._log_file.close()
        bus.publish("engine", "stopped")

    # ----------------------------------------------------- introspection api
    # These two endpoints are llama.cpp-specific — NOT part of the OpenAI-
    # compatible surface — which is exactly why they live behind the adapter.

    async def props(self) -> dict:
        """GET /props: the server's EFFECTIVE configuration (total_slots,
        default generation settings, chat-template capabilities)."""
        response = await self._client.get("/props", timeout=10.0)
        response.raise_for_status()
        return response.json()

    async def slots(self) -> list[dict]:
        """GET /slots: every slot's live state, including its real n_ctx —
        the number that exposes silent static partitioning."""
        response = await self._client.get("/slots", timeout=10.0)
        response.raise_for_status()
        return response.json()

    # -------------------------------------------------------------- affinity
    def _slot_for(self, cache_key: Optional[str]) -> Optional[int]:
        """Map a conversation's stable cache_key to a sticky slot id —
        WITHOUT ever sharing a live slot between two conversations.

        A key keeps its slot across requests (the warm-prefix economy).
        But if that slot is currently generating for a DIFFERENT key, the
        conversation is re-pinned to a free slot — its cached prefix is
        lost once, and nothing is corrupted. With no free slot at all the
        request goes UNPINNED (the server queues it wherever it likes),
        which is the same cache loss and still no sharing. Fresh keys
        prefer a free slot over blind round-robin for the same reason.
        Requests without a key get no pin.
        """
        if cache_key is None or not self.slot_pinning_ok:
            return None
        total = max(1, self._total_slots)
        # Free = not live for another conversation on OUR ledger, and not
        # reported busy by the server unless the busy one is ours.
        free = [s for s in range(total)
                if (s not in self._live or self._live[s][0] == cache_key)
                and (s not in self._server_busy or self._last_key.get(s) == cache_key)]
        pinned = self._slot_of.get(cache_key)
        if pinned is not None:
            if pinned in free:
                return pinned                 # idle, or busy with THIS conversation
            if not free:
                logger.info("every slot is live for another conversation; "
                            "%s goes unpinned this once", cache_key)
                return None
            logger.info("slot %d is live for another conversation; re-pinning "
                        "%s to slot %d", pinned, cache_key, free[0])
            self._slot_of[cache_key] = free[0]
            return free[0]
        # Bound the map: a long-lived app sees many conversations, but
        # only the recent ones matter. At 4x the slot count, evict the
        # oldest mapping (its cache is long gone anyway).
        if len(self._slot_of) >= total * 4:
            oldest = next(iter(self._slot_of))
            del self._slot_of[oldest]
        if not free:
            return None                       # nothing idle: unpinned, uncorrupted
        # Round-robin among the FREE slots, so consecutive new
        # conversations spread out instead of piling onto slot 0.
        slot = free[self._next_slot % len(free)]
        self._next_slot += 1
        self._slot_of[cache_key] = slot
        return slot

    def _acquire(self, slot: Optional[int], cache_key: Optional[str]) -> None:
        """Record that `cache_key` now has a request live on `slot`."""
        if slot is None or cache_key is None:
            return
        holder, count = self._live.get(slot, (cache_key, 0))
        self._live[slot] = (cache_key, count + 1)

    def _release(self, slot: Optional[int], cache_key: Optional[str]) -> None:
        """The request finished (or was cancelled): free its claim."""
        if slot is None or cache_key is None or slot not in self._live:
            return
        holder, count = self._live[slot]
        if holder != cache_key:
            return
        if count <= 1:
            del self._live[slot]
        else:
            self._live[slot] = (holder, count - 1)

    # ------------------------------------------------------------ generation
    def _payload(self, req: GenerationRequest, stream: bool,
                 slot: Optional[int] = None, pin: bool = True) -> dict:
        """Translate a GenerationRequest to the wire format. `slot` is the
        affinity decision already made by the caller (stream/complete
        claim it for the request's lifetime); `pin=False` skips affinity."""
        payload: dict = {
            "messages": req.messages,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "stream": stream,
        }
        # Native tool calling, when the request wants it.
        if req.tools:
            payload["tools"] = req.tools
        # Sampling knobs: only the ones set (None keeps the server's own
        # default, so an untouched request behaves exactly as before).
        for field_name in ("top_p", "top_k", "min_p", "repeat_penalty",
                           "presence_penalty"):
            value = getattr(req, field_name)
            if value is not None:
                payload[field_name] = value
        if req.reasoning_budget is not None:
            payload["reasoning_budget"] = req.reasoning_budget
        # Template knobs (e.g. enable_thinking=False) pass straight to the
        # model's own chat template via --jinja.
        if req.template_kwargs:
            payload["chat_template_kwargs"] = req.template_kwargs
        # The affinity fields. These are llama.cpp-specific extras — a cloud
        # endpoint would 400 on them, but this adapter only ever talks to
        # our own llama-server, so they are always safe here.
        if pin and slot is None:
            slot = self._slot_for(req.cache_key)  # callers that didn't decide
        if slot is not None:
            payload["id_slot"] = slot             # pin to this slot
            # Reuse the slot's cached prefix ONLY for the conversation
            # that built it (see _last_key); a different conversation gets
            # a clean prefill rather than a partial-prefix restore.
            payload["cache_prompt"] = self._last_key.get(slot) == req.cache_key
            self._last_key[slot] = req.cache_key or ""
        # Ask the server to include its own timing block in the stream —
        # its decode tok/s is measured at the sampler and is the honest
        # number (tokens over wall-clock would fold prefill and network in
        # and read misleadingly low).
        if stream:
            payload["timings_per_token"] = True
        return payload

    def _record_timings(self, req: GenerationRequest, chunk: dict) -> None:
        """Copy a response chunk's `timings` block into the request's stats.

        llama-server reports: prompt_n / predicted_n (token counts) and
        prompt_per_second / predicted_per_second (its own measured rates).
        Later chunks overwrite earlier ones — the final chunk carries the
        totals for the whole generation.
        """
        timings = chunk.get("timings")
        if not isinstance(timings, dict):
            return
        req.stats.update({
            "prompt_tokens": timings.get("prompt_n"),
            "generated_tokens": timings.get("predicted_n"),
            "prefill_tps": timings.get("prompt_per_second"),
            "decode_tps": timings.get("predicted_per_second"),
            "tps_source": "engine",       # the engine's own timers said so
        })
        # MTP's own counters, when the build reports them: how many tokens
        # the draft head PROPOSED and how many the full model ACCEPTED.
        # These are cumulative server counters, so callers compare deltas.
        if "draft_n" in timings:
            req.stats["draft_n"] = timings.get("draft_n")
            req.stats["draft_n_accepted"] = timings.get("draft_n_accepted")

    async def stream(self, req: GenerationRequest) -> AsyncIterator[str]:
        """Send a conversation, yield text fragments as they arrive.

        Consumes llama-server's SSE and yields only the text, so callers
        never see the wire format. Cancellation contract: cancelling the
        task that consumes this generator closes the HTTP stream, which
        tells llama-server to stop generating and free the slot — that is
        what the scheduler's preemption relies on.
        """
        # Decide the slot ONCE and hold it for the whole stream, so no
        # other conversation is pinned onto it meanwhile (see _slot_for).
        slot = self._slot_for(req.cache_key)
        self._acquire(slot, req.cache_key)
        try:
            async with self._client.stream(
                "POST", "/v1/chat/completions",
                json=self._payload(req, stream=True, slot=slot, pin=False),
            ) as response:
                # Read the error body BEFORE raising, or httpx complains that
                # the body was never read on a streaming response.
                if response.status_code >= 400:
                    await response.aread()
                    response.raise_for_status()
                async for line in response.aiter_lines():
                    # SSE frames look like:  data: {...}
                    # Blank lines separate frames; ignore anything else
                    # (including ": heartbeat" comments).
                    if not line.startswith("data: "):
                        continue
                    data = line[len("data: "):]
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        logger.warning("unparseable SSE frame: %r", data)
                        continue
                    # Timing telemetry rides on ordinary chunks (the final one
                    # carries the totals) — harvest it before the text.
                    self._record_timings(req, chunk)
                    # The text lives at choices[0].delta.content, and any level
                    # can be absent on keep-alive or role-announcement frames —
                    # chained .get() with defaults avoids a KeyError storm.
                    delta = (chunk.get("choices") or [{}])[0].get("delta", {})
                    if text := delta.get("content"):
                        yield text
        finally:
            self._release(slot, req.cache_key)

    async def complete(self, req: GenerationRequest) -> str:
        """Non-streaming path: one POST, one JSON body, the full reply."""
        slot = self._slot_for(req.cache_key)
        self._acquire(slot, req.cache_key)
        try:
            response = await self._client.post(
                "/v1/chat/completions",
                json=self._payload(req, stream=False, slot=slot, pin=False),
            )
            # If the build rejects id_slot (older/newer API), retry unpinned
            # once and remember — the handshake surfaces this honestly.
            if response.status_code == 400 and "id_slot" in response.text and self.slot_pinning_ok:
                logger.warning("this llama-server build rejects id_slot; disabling slot pinning")
                self.slot_pinning_ok = False
                response = await self._client.post(
                    "/v1/chat/completions", json=self._payload(req, stream=False, pin=False)
                )
        finally:
            self._release(slot, req.cache_key)
        response.raise_for_status()
        body = response.json()
        # Non-streaming responses carry the same timing block; harvest it.
        self._record_timings(req, body)
        # Same defensive extraction as the streaming path.
        message = (body.get("choices") or [{}])[0].get("message", {})
        return message.get("content") or ""

    # ------------------------------------------------------------------ misc
    async def capabilities(self) -> EngineCapabilities:
        """Return the handshake's measurements (run once, then frozen)."""
        # Import here to avoid a circular import (handshake drives THIS
        # adapter to make its measurements).
        from seymour.engine.handshake import run_handshake
        if self._caps is None:
            self._caps = await run_handshake(self)
        return self._caps

    async def stats(self) -> GenerationStats:
        """Live slot occupancy from /slots, for the status UI and floor
        accounting. Degrades to empty stats if the endpoint hiccups."""
        try:
            slot_list = await self.slots()
        except httpx.HTTPError:
            return GenerationStats(slots_total=self._total_slots)
        # Refresh the server-side occupancy the affinity logic consults
        # (the status panel polls this every 2 s, so it stays current).
        self._server_busy = {
            int(s["id"]) for s in slot_list
            if s.get("is_processing") and isinstance(s.get("id"), int)}
        return GenerationStats(
            slots_total=len(slot_list) or self._total_slots,
            # is_processing marks a slot mid-generation.
            slots_busy=sum(1 for s in slot_list if s.get("is_processing")),
            slots=[
                {
                    "id": s.get("id"),
                    "busy": bool(s.get("is_processing")),
                    # How much of the slot's context is occupied right now.
                    "tokens": (s.get("prompt") or {}).get("n_tokens", 0)
                              if isinstance(s.get("prompt"), dict) else 0,
                }
                for s in slot_list
            ],
        )
