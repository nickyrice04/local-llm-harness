"""The MLX engine: an mlx-lm or mlx-vlm server child, behind the same adapter.

Same shape as engine/llamacpp.py, same rules: launch a child process on a
loopback port, wait until it really answers, translate GenerationRequest
to the server's OpenAI-compatible wire format, stream text deltas, report
what the server measured (never what we hoped), and free the memory on
stop. Everything above this file — handshake, scheduler, agent — is
unchanged; that is the whole point of the adapter seam.

Two servers, one adapter, because the two things a person wants from
MLX on Apple Silicon live in different programs today (mlx-lm 0.31 /
mlx-vlm 0.6, September 2026):

    mlx-lm server   continuous batching (--decode-concurrency) and an
                    LRU prompt cache that matches PREFIXES across
                    requests — several streams at once, warm reuse of a
                    conversation's history. No MTP for Qwen3.5/3.8: the
                    drafter's model_type (qwen3_5_mtp) is unknown to it.
    mlx-vlm server  MTP speculative decoding with the drafter checkpoint
                    (--draft-kind mtp): the fastest single stream this
                    model family can do on this hardware. One request at
                    a time — no batching.

"auto" picks by measurement (see _choose_server) and the Models tab can
override it. Either way the handshake measures the result: slots, real
context, caching, overlap, and whether MTP actually drafts.

What an MLX server does NOT have, and how this adapter stays honest
about it: there are no /props and /slots endpoints, so props()/slots()
are synthesized from what this process knows — the launch flags and its
own in-flight ledger. Those are facts about the CLIENT's view; the
handshake's timing probes then measure the server itself.
"""

import asyncio
import json
import logging
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import AsyncIterator, Optional

import httpx

from seymour.config import settings
from seymour.engine import mlxinfo
from seymour.engine.guard import guarded
from seymour.engine.adapter import (EngineAdapter, EngineCapabilities,
                                    GenerationRequest, GenerationStats)
from seymour.events import bus

logger = logging.getLogger(__name__)

# How long a cold load may take before we call it failed. A 28 GB
# checkpoint pages in at disk speed; the dead-child check below turns a
# crash into an immediate error long before this.
LOAD_TIMEOUT_S = 900.0


class MlxEngine(EngineAdapter):
    """A running mlx-lm / mlx-vlm server, owned by this process."""

    def __init__(self, model_path=None) -> None:
        # The checkpoint DIRECTORY to serve (config.json + safetensors).
        self._model_path = Path(model_path or settings.model_path)
        self._process: Optional[subprocess.Popen] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._caps: Optional[EngineCapabilities] = None
        self._external = False
        # Decided at start(): which server, whether a drafter rides along.
        self._server_kind = "mlx-lm"
        self._drafter: Optional[Path] = None
        self._mtp_requested = False
        # What the launch flags promise; the handshake measures the truth.
        self._total_slots = 1
        self._ctx = settings.ctx_size
        # The in-flight ledger: request id → started-at. slots() maps
        # its size onto "busy slots" for the handshake's overlap probe
        # and the status panel — the honest client-side view.
        self._live: dict[int, float] = {}
        self._next_id = 0
        # MLX servers have no slot ids to pin; the scheduler's affinity
        # logic simply has nothing to do here (mlx-lm's prefix cache
        # matches by content, which is better than pinning).
        self.slot_pinning_ok = False
        # Draft counters from these servers describe ONE request; the
        # handshake's MTP probe must not subtract a warmup's numbers.
        self.draft_counters_cumulative = False
        self._log_file = None

    # ------------------------------------------------------------- lifecycle
    def _choose_server(self, chosen: dict) -> str:
        """Which server to launch. An explicit setting wins; "auto" uses
        the drafter's presence and the MTP setting.

        Measured on the M5 Max, Qwen3.8-27B-8bit, 2026-09-03 (NOTES.md):

            mlx-lm          17 tok/s per stream; THREE streams at 16.7 each
                            overlapping for 11.9 of 12.6 s (47.7 aggregate);
                            a request arriving mid-stream: first token in
                            0.56 s; a warm 2k prompt: 0.22 s instead of 2.5 s.
            mlx-vlm + MTP   27-32 tok/s solo (drafts ~50% accepted); three
                            streams 19/11/11 with 4.8 s of overlap (batch-
                            at-a-time); a request arriving mid-batch waits:
                            first token in 6.7 s.

        Seymour's whole design is a person and an agent sharing one model
        without taking turns, so "auto" launches mlx-lm. MTP is the right
        call for someone working alone who wants the fastest single reply
        — turning the MTP setting ON (with a drafter present) asks for it.
        """
        if chosen.get("mlx_server") in ("mlx-lm", "mlx-vlm"):
            return chosen["mlx_server"]
        return "mlx-vlm" if (self._drafter and chosen.get("mtp") == "auto") else "mlx-lm"

    def _command(self, chosen: dict) -> list[str]:
        """The launch command. Every flag relied on is passed explicitly."""
        python = sys.executable
        host, port = settings.llama_host, str(settings.mlx_port)
        decode = int(chosen.get("mlx_decode_concurrency") or settings.mlx_decode_concurrency)
        if self._server_kind == "mlx-vlm":
            # --host: mlx-vlm defaults to 0.0.0.0 — loopback must be
            # explicit. --max-num-seqs caps the batched pending set (its
            # default is unbounded). --max-kv-size is ENFORCED by the
            # server (PromptTooLongError), so the context cap is real.
            cmd = [python, "-m", "mlx_vlm", "server", "--model", str(self._model_path),
                   "--host", host, "--port", port,
                   "--max-num-seqs", str(decode),
                   "--max-kv-size", str(self._ctx),
                   "--max-tokens", "32768"]          # per-request default; each request sets its own
            if self._drafter is not None:
                info = mlxinfo.read_info(self._drafter)
                block = info.draft_block_size or int(chosen.get("mlx_draft_tokens") or settings.mlx_draft_tokens)
                cmd += ["--draft-model", str(self._drafter), "--draft-kind", "mtp",
                        "--draft-block-size", str(block)]
                self._mtp_requested = True
            self._total_slots = decode
            return cmd
        cache_bytes = int(chosen.get("mlx_prompt_cache_gb") or settings.mlx_prompt_cache_gb) * 1024 ** 3
        prompt = int(chosen.get("mlx_prompt_concurrency") or settings.mlx_prompt_concurrency)
        cmd = [python, "-m", "mlx_lm", "server", "--model", str(self._model_path),
               "--host", host, "--port", port,
               "--decode-concurrency", str(decode),
               "--prompt-concurrency", str(prompt),
               "--prompt-cache-size", "16",
               "--prompt-cache-bytes", str(cache_bytes),
               # Measured (2026-09-03): with the default 2048-token prefill
               # step, every other stream got ONE token per step while a
               # 7k prompt was prefilled — 2.3 s gaps. Halving the step
               # halves the stall; the prefill-throughput cost is small.
               "--prefill-step-size", "1024",
               "--max-tokens", "32768",              # per-request default; each request sets its own
               "--log-level", "INFO"]
        self._total_slots = decode
        return cmd

    async def start(self, adopt: str = "always") -> None:
        """Launch the MLX server and block until it answers a completion."""
        self._client = httpx.AsyncClient(base_url=settings.mlx_url, timeout=LOAD_TIMEOUT_S)
        occupied = await self._port_answers()
        if occupied:
            # Something already serves on our port. Unlike llama-server it
            # cannot tell us which model it holds, so adopting is only
            # allowed at boot ("always"); an explicit Load refuses.
            if adopt != "always":
                await self._client.aclose()
                raise RuntimeError(
                    f"port {settings.mlx_port} is already in use by a server Seymour did "
                    f"not start. Stop it (or change SEYMOUR_MLX_PORT) and try again.")
            self._external = True
            # Its launch flags are unknowable (no /props), so the saved
            # decode concurrency is the best prior for how many sequences
            # it runs at once; the handshake's overlap probe then measures
            # the truth. Without this an adopted server counted as ONE
            # slot and probe 4 was skipped — serial mode by default,
            # which is a guess dressed as a measurement.
            try:
                from seymour.models_manager.registry import engine_settings
                self._total_slots = max(1, int(engine_settings()["mlx_decode_concurrency"]))
            except Exception:
                self._total_slots = 1
            logger.warning("adopting an already-running MLX server on %s — Seymour did "
                           "not launch it and will not stop it (assuming %d sequences "
                           "until the handshake measures)", settings.mlx_url, self._total_slots)
            bus.publish("engine", "ready", model=self.model_name, external=True)
            return

        if not mlxinfo.is_mlx_model_dir(self._model_path):
            raise FileNotFoundError(
                f"Not an MLX checkpoint folder: {self._model_path} (needs config.json "
                f"and *.safetensors). Download one in the Models tab.")
        info = mlxinfo.read_info(self._model_path)
        if info.is_drafter:
            raise ValueError(f"{self._model_path.name} is an MTP drafter, not a model — "
                             f"load its base model; the drafter is used automatically.")
        from seymour.models_manager.registry import engine_settings
        chosen = engine_settings()
        self._ctx = min(int(chosen["ctx_size"]), info.max_context or int(chosen["ctx_size"]))
        self._drafter = (mlxinfo.find_drafter(self._model_path)
                         if chosen.get("mtp") != "off" else None)
        self._server_kind = self._choose_server(chosen)
        if self._server_kind == "mlx-lm":
            self._drafter = None                     # mlx-lm cannot use it
        cmd = self._command(chosen)
        logger.info("starting %s: %s", self._server_kind, " ".join(cmd))
        bus.publish("engine", "loading", model=self.model_name)

        log_dir = settings.data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._log_file = open(log_dir / "mlx-server.log", "ab")
        env = dict(os.environ)
        env.setdefault("HF_HUB_OFFLINE", "1")        # never phone home for a local dir
        if self._server_kind == "mlx-vlm":
            # Automatic prefix caching has no CLI flag; ask for it and let
            # probe 3 say whether it works on this hybrid model.
            env.setdefault("APC_ENABLED", "1")
        # Its own process group, so stop() can take the whole tree down.
        # Under the child guard (engine/guard.py): if this process dies
        # without running stop() — killed hard, crashed — the guard takes
        # the server down within a second instead of leaving 28 GB behind.
        self._process = subprocess.Popen(guarded(cmd), stdout=self._log_file,
                                         stderr=subprocess.STDOUT, env=env,
                                         start_new_session=True)
        try:
            await self._wait_until_ready()
        except Exception:
            await self.stop()
            raise
        bus.publish("engine", "ready", model=self.model_name, external=False)
        logger.info("%s ready on %s (slots=%d, ctx=%d, drafter=%s)", self._server_kind,
                    settings.mlx_url, self._total_slots, self._ctx,
                    self._drafter.name if self._drafter else "none")

    async def _port_answers(self) -> bool:
        """Does anything speak HTTP on our port already?"""
        try:
            await self._client.get("/v1/models", timeout=1.0)
            return True
        except httpx.RequestError:
            return False

    async def _wait_until_ready(self) -> None:
        """Poll until the server both listens AND has the weights loaded.

        mlx servers bind their port before (or while) loading the model,
        so a TCP answer proves nothing; the only honest readiness test is
        a real one-token completion, which blocks until the model is in
        memory. The dead-child check runs every pass: an OOM death is an
        immediate clear error, not a fifteen-minute silence.
        """
        deadline = asyncio.get_running_loop().time() + LOAD_TIMEOUT_S
        listening = False
        while asyncio.get_running_loop().time() < deadline:
            if self._process and self._process.poll() is not None:
                raise RuntimeError(
                    f"{self._server_kind} exited during startup (code "
                    f"{self._process.returncode}). Out of memory, or an unsupported "
                    f"model type? See {settings.data_dir}/logs/mlx-server.log")
            if not listening:
                listening = await self._port_answers()
                if not listening:
                    await asyncio.sleep(1.0)
                    continue
            try:
                probe = GenerationRequest(messages=[{"role": "user", "content": "Say ok."}],
                                          max_tokens=1, temperature=0.0)
                await self.complete(probe)
                return
            except httpx.HTTPStatusError as error:
                # mlx-lm answers /health before (and after a FAILED) load;
                # a generation error while the socket is up is the load
                # failing, not the load pending.
                body = error.response.text[:300] if error.response is not None else ""
                if error.response is not None and error.response.status_code in (400, 404, 500):
                    raise RuntimeError(
                        f"{self._server_kind} could not serve {self._model_path.name}: "
                        f"{body or error}. See {settings.data_dir}/logs/mlx-server.log")
                await asyncio.sleep(2.0)
            except httpx.HTTPError as error:
                logger.debug("readiness probe not yet answered: %s", error)
                await asyncio.sleep(2.0)
        raise TimeoutError(f"{self._server_kind} not ready after {LOAD_TIMEOUT_S:.0f}s")

    @property
    def external(self) -> bool:
        return self._external

    @property
    def model_name(self) -> str:
        """The checkpoint folder's name (what the Models tab shows)."""
        return self._model_path.name

    @property
    def model_path(self):
        return self._model_path

    @property
    def engine_label(self) -> str:
        """For the mode banner: which program is serving, and how."""
        version = ""
        try:
            if self._server_kind == "mlx-vlm":
                import mlx_vlm
                version = getattr(mlx_vlm, "__version__", "")
            else:
                import mlx_lm
                version = getattr(mlx_lm, "__version__", "")
        except ImportError:
            pass
        extra = " + MTP drafter" if self._drafter else ""
        return f"{self._server_kind} {version}{extra}".strip()

    async def stop(self) -> None:
        """Shut the server down and release its memory (the whole process
        group — mlx servers spawn worker threads/processes)."""
        if self._external:
            logger.info("detaching from external MLX server (left running)")
        if self._client:
            await self._client.aclose()
        if self._process and self._process.poll() is None:
            try:
                os.killpg(self._process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                with contextlib_suppress(ProcessLookupError):
                    os.killpg(self._process.pid, signal.SIGKILL)
            logger.info("%s stopped", self._server_kind)
        if self._log_file:
            self._log_file.close()
            self._log_file = None
        bus.publish("engine", "stopped")

    # ----------------------------------------------------- introspection api
    async def props(self) -> dict:
        """What /props would say if the server had one: the launch facts."""
        return {
            "total_slots": self._total_slots,
            "build_info": self.engine_label,
            "model_path": str(self._model_path),
            "chat_template_caps": {"supports_tools": True},
            "modalities": {"vision": False},   # text-only serving until measured otherwise
        }

    async def slots(self) -> list[dict]:
        """Synthesized slots: N launch slots, the first len(live) of them
        marked processing. n_ctx is the per-request window cap."""
        busy = len(self._live)
        return [{"id": i, "n_ctx": self._ctx, "is_processing": i < busy}
                for i in range(self._total_slots)]

    # ------------------------------------------------------------- requests
    def _payload(self, req: GenerationRequest, stream: bool) -> dict:
        """GenerationRequest → the server's chat-completions fields."""
        payload: dict = {
            "messages": req.messages,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "stream": stream,
        }
        if req.tools:
            payload["tools"] = req.tools
        for ours, theirs in (("top_p", "top_p"), ("top_k", "top_k"), ("min_p", "min_p"),
                             ("repeat_penalty", "repetition_penalty"),
                             ("presence_penalty", "presence_penalty")):
            value = getattr(req, ours)
            if value is not None:
                payload[theirs] = value
        if req.repeat_penalty is not None:
            # llama.cpp looks back 64 tokens; these servers default to 20.
            payload["repetition_context_size"] = 64
        kwargs = dict(req.template_kwargs or {})
        if self._server_kind == "mlx-vlm":
            # mlx-vlm: `model` must be the loaded path EXACTLY or the
            # server swaps models; `seed` defaults to 0 (deterministic
            # sampling) so a sampled request gets a fresh one; thinking
            # on/off is a top-level field; a thinking budget is refused
            # while a drafter is loaded, so it is sent only without one.
            payload["model"] = str(self._model_path)
            if req.temperature > 0:
                payload["seed"] = random.randrange(1, 2 ** 31)
            if "enable_thinking" in kwargs:
                payload["enable_thinking"] = bool(kwargs.pop("enable_thinking"))
            if (req.reasoning_budget is not None and req.reasoning_budget >= 0
                    and self._drafter is None):
                payload["thinking_budget"] = req.reasoning_budget
            if req.reasoning_budget == 0:
                payload["enable_thinking"] = False
            if kwargs:
                payload["chat_template_kwargs"] = kwargs   # ignored if unknown
        elif kwargs:
            payload["chat_template_kwargs"] = kwargs
            # mlx-lm has no reasoning-budget field; a budget of 0 is
            # honoured the only way it can be — thinking off.
            if req.reasoning_budget == 0:
                payload["chat_template_kwargs"] = {**kwargs, "enable_thinking": False}
        elif req.reasoning_budget == 0:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if stream:
            payload["stream_options"] = {"include_usage": True}
        return payload

    def _record_usage(self, req: GenerationRequest, body: dict, started: float,
                      first_token_at: Optional[float], generated: int) -> None:
        """Fill req.stats from the server's usage block plus our clock.

        The server reports token COUNTS; the rates are ours (wall-clock
        from first token to last), and tps_source says so.
        """
        usage = body.get("usage") if isinstance(body, dict) else None
        timings = body.get("timings") if isinstance(body, dict) else None
        if isinstance(timings, dict) and timings.get("predicted_n"):
            # mlx-vlm speaks llama-server's timings dialect: its own
            # sampler-measured rates, plus MTP draft counters. These are
            # PER REQUEST (see draft_counters_cumulative), never deltas.
            req.stats.update({
                "prompt_tokens": timings.get("prompt_n"),
                "generated_tokens": timings.get("predicted_n"),
                "prefill_tps": timings.get("prompt_per_second"),
                "decode_tps": timings.get("predicted_per_second"),
                "tps_source": "engine",
            })
            if "draft_n" in timings:
                req.stats["draft_n"] = timings.get("draft_n") or 0
                req.stats["draft_n_accepted"] = timings.get("draft_n_accepted") or 0
            details = (usage or {}).get("prompt_tokens_details") or {}
            if details.get("cached_tokens") is not None:
                req.stats["cached_tokens"] = details["cached_tokens"]
            return
        now = time.perf_counter()
        prompt_tokens = (usage or {}).get("prompt_tokens")
        completion_tokens = (usage or {}).get("completion_tokens") or generated
        details = (usage or {}).get("prompt_tokens_details") or {}
        stats = {
            "prompt_tokens": prompt_tokens,
            "generated_tokens": completion_tokens,
            "tps_source": "measured",
        }
        if first_token_at is not None:
            prefill_s = max(first_token_at - started, 1e-6)
            decode_s = max(now - first_token_at, 1e-6)
            if prompt_tokens:
                stats["prefill_tps"] = round(prompt_tokens / prefill_s, 1)
            if completion_tokens and completion_tokens > 1:
                stats["decode_tps"] = round((completion_tokens - 1) / decode_s, 1)
        if details.get("cached_tokens") is not None:
            stats["cached_tokens"] = details["cached_tokens"]
        # Draft counters, when the server reports them (mlx-vlm + MTP).
        for key in ("draft_n", "draft_n_accepted", "num_draft_tokens", "num_accepted_tokens"):
            if key in (usage or {}):
                stats[key] = usage[key]
        if "num_draft_tokens" in stats and "draft_n" not in stats:
            stats["draft_n"] = stats["num_draft_tokens"]
            stats["draft_n_accepted"] = stats.get("num_accepted_tokens", 0)
        req.stats.update({k: v for k, v in stats.items() if v is not None})

    def _enter(self) -> int:
        """Reserve a ledger id. NOT yet live: a request the server has
        merely queued must not count as a busy slot, or the overlap probe
        would see concurrency that is not there (mlx-lm serialises
        requests when a draft model is loaded; the second one receives
        no bytes until the first ends)."""
        self._next_id += 1
        return self._next_id

    def _mark_live(self, rid: int) -> None:
        """Tokens are flowing for this request: it now occupies a slot."""
        self._live.setdefault(rid, time.perf_counter())

    def _leave(self, rid: int) -> None:
        self._live.pop(rid, None)

    async def stream(self, req: GenerationRequest) -> AsyncIterator[str]:
        """Send a conversation, yield text fragments as they arrive.

        Cancelling the consuming task closes the HTTP stream; the server
        notices the closed socket and stops generating — measured for
        both servers by the handshake's cancellation is the scheduler's
        preemption mechanism, same as llama-server.
        """
        rid = self._enter()
        started = time.perf_counter()
        first_token_at: Optional[float] = None
        generated = 0
        last: dict = {}
        try:
            async with self._client.stream("POST", "/v1/chat/completions",
                                           json=self._payload(req, stream=True)) as response:
                if response.status_code >= 400:
                    await response.aread()
                    response.raise_for_status()
                if "text/event-stream" not in (response.headers.get("content-type") or "") \
                        and response.status_code == 200:
                    # A JSON body on a stream request: the server declined
                    # to stream (mlx-lm does this for some errors) — read it
                    # as a whole reply rather than parsing nothing.
                    raw = await response.aread()
                    try:
                        body = json.loads(raw)
                    except json.JSONDecodeError:
                        body = {}
                    if body.get("error"):
                        raise httpx.HTTPStatusError(str(body["error"]), request=response.request,
                                                    response=response)
                    message = (body.get("choices") or [{}])[0].get("message", {}) or {}
                    text = message.get("content") or ""
                    if text:
                        self._mark_live(rid)
                        first_token_at = time.perf_counter()
                        generated = (body.get("usage") or {}).get("completion_tokens") or 1
                        last = body
                        yield text
                    return
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[len("data: "):]
                    if data.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        logger.warning("unparseable SSE frame: %r", data[:200])
                        continue
                    if chunk.get("error"):
                        # Both servers report failures after the headers
                        # in-band (mlx-vlm: data: {"error": …}).
                        raise RuntimeError(f"{self._server_kind}: {chunk['error']}")
                    if chunk.get("usage"):
                        last = chunk
                    delta = (chunk.get("choices") or [{}])[0].get("delta", {}) or {}
                    text = delta.get("content")
                    if text:
                        if first_token_at is None:
                            first_token_at = time.perf_counter()
                            self._mark_live(rid)
                        generated += 1
                        yield text
        except httpx.RemoteProtocolError as error:
            # mlx-lm closes the socket WITHOUT a response when a request
            # fails validation (a bad field type, a missing messages
            # list) — name it, so the log says what happened.
            raise RuntimeError(f"{self._server_kind} closed the connection without a reply "
                               f"(a request-validation failure — see mlx-server.log): {error}"
                               ) from error
        finally:
            self._leave(rid)
            self._record_usage(req, last, started, first_token_at, generated)

    async def complete(self, req: GenerationRequest) -> str:
        """The full reply as one string — gathered from the STREAM, so the
        in-flight ledger (and therefore the handshake's overlap probe,
        which uses complete()) sees when decoding really starts."""
        pieces = []
        async for piece in self.stream(req):
            pieces.append(piece)
        return "".join(pieces)

    # ------------------------------------------------------------------ misc
    async def capabilities(self) -> EngineCapabilities:
        from seymour.engine.handshake import run_handshake
        if self._caps is None:
            self._caps = await run_handshake(self)
        return self._caps

    async def stats(self) -> GenerationStats:
        slot_list = await self.slots()
        return GenerationStats(
            slots_total=len(slot_list),
            slots_busy=sum(1 for s in slot_list if s["is_processing"]),
            slots=[{"id": s["id"], "busy": s["is_processing"], "tokens": 0} for s in slot_list],
        )


class contextlib_suppress:
    """A tiny contextlib.suppress stand-in (keeps the import list short)."""

    def __init__(self, *exceptions):
        self._exceptions = exceptions

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None and issubclass(exc_type, self._exceptions)
