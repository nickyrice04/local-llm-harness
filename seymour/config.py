"""Configuration, loaded once at import.

Everything environment-specific lives here. Nothing else in the codebase
reads os.environ — when a value needs to vary, it gets added to this class
and the rest of the code just reads an attribute.

Values can be overridden by environment variables (prefixed SEYMOUR_) or a
.env file, so you never edit this file to change a port:

    SEYMOUR_MODEL_PATH=/somewhere/else.gguf
    SEYMOUR_N_SLOTS=2
"""

# Path gives us portable, readable filesystem paths (better than raw strings).
from pathlib import Path

# BaseSettings is a pydantic class that fills its fields from the environment,
# validating types as it goes — a typo like SEYMOUR_N_SLOTS=four fails loudly.
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Every knob Seymour has, with the reasoning attached to each."""

    # Read a .env file if present. The repo's .gitignore keeps that file out
    # of git — which is exactly why secrets are allowed to live in it.
    model_config = SettingsConfigDict(env_file=".env", env_prefix="SEYMOUR_")

    # --- Where things live ---------------------------------------------------
    # The chat model weights (.gguf). The default points at the Models/ folder
    # sitting next to this repo; override with SEYMOUR_MODEL_PATH in .env.
    model_path: Path = Path(__file__).resolve().parent.parent / "Models" / "Qwen3.6-35B-A3B-Q8_0.gguf"
    # Where downloaded models are stored (the model manager writes here).
    models_dir: Path = Path(__file__).resolve().parent.parent / "Models"
    # Seymour's own state: database, soul file, agent workspace, logs.
    data_dir: Path = Path.home() / ".seymour"

    # --- llama-server (the inference engine) ---------------------------------
    llama_host: str = "127.0.0.1"   # loopback ONLY. Never 0.0.0.0 — that would
    llama_port: int = 8080          # expose the model on your network with no
                                    # authentication.
    n_slots: int = 4                # --parallel: how many requests may decode
                                    # at once. The scheduler divides these
                                    # between the three tiers.
    ctx_size: int = 32768           # --ctx-size: TOTAL context. With
                                    # --kv-unified this is a shared pool, not
                                    # divided per slot — the handshake verifies.
    cache_type_k: str = "q8_0"      # --cache-type-k: 8-bit KV keys. Halves KV
                                    # memory at negligible quality cost.
                                    # (NOT q4 — quality falls off a cliff.)

    # --- MTP: self-speculative decoding -------------------------------------
    # When the weights ship Multi-Token-Prediction heads (read from the
    # GGUF header, never assumed), llama.cpp can use them as a built-in
    # draft: the MTP head proposes the next few tokens and the full model
    # verifies them in ONE pass. Accepted tokens cost almost nothing, so
    # decode speeds up; rejected ones cost a little extra compute — which
    # is exactly why the handshake MEASURES the result instead of
    # trusting it — and the measurement decided the DEFAULT: solo
    # structured output gains ~1.67x, but under 3-way concurrency each
    # stream drops ~53 → ~42 tok/s (verification competes for the batch
    # dimension concurrent sequences use). Concurrency is Seymour's whole
    # point, so MTP ships OFF; the Models tab's Engine card turns it on
    # for anyone whose workload is mostly solo.
    mtp: str = "off"                # "auto" (use it when present) | "off"
    mtp_draft_n: int = 3            # --spec-draft-n-max: tokens drafted per
                                    # step. 3 is llama.cpp's default and the
                                    # usual sweet spot for a 1-layer head.

    # --- MLX (Apple Silicon) — the second engine, same logic ------------------
    # An MLX checkpoint is a DIRECTORY (config.json + safetensors); choosing
    # one in the Models tab launches an MLX server child instead of
    # llama-server, on ITS port, behind the same adapter/handshake/
    # scheduler. Two servers exist: mlx-lm (continuous batching + a prompt
    # cache; no MTP for this model family) and mlx-vlm (MTP self-
    # speculative decoding via a drafter checkpoint; batch-at-a-time, so a
    # request arriving mid-batch waits). Measured 2026-09-03: mlx-lm 17
    # tok/s per stream and three streams at 16.7 each with full overlap;
    # mlx-vlm+MTP 27-32 tok/s solo but 6.7 s first-token for a late
    # arrival. "auto" = mlx-lm (overlap is the design); MTP on = mlx-vlm.
    mlx_port: int = 8082            # never the llama port (8080) or the
                                    # embedder's (8081): all three may be
                                    # configured, only one runs at a time
    mlx_server: str = "auto"        # "auto" | "mlx-lm" | "mlx-vlm"
    mlx_decode_concurrency: int = 4 # mlx-lm: sequences decoded per step
    mlx_prompt_concurrency: int = 2 # mlx-lm: prompts prefilled per step
    mlx_prompt_cache_gb: int = 12   # mlx-lm: KV prefix cache budget (GB)
    mlx_draft_tokens: int = 3       # mlx-vlm: MTP block size (drafter's own
                                    # block_size wins when it says otherwise)

    # --- The embedding engine (optional, for memory/RAG) ---------------------
    # A tiny second model dedicated to turning text into vectors. It is NOT a
    # second copy of the chat model (that would violate the one-instance rule);
    # it is a ~100 MB specialist that leaves the bandwidth budget untouched.
    # If the file is absent, memory falls back to a dependency-free embedder.
    embed_model_path: Path = Path(__file__).resolve().parent.parent / "Models" / "embeddings.gguf"
    embed_port: int = 8081          # its own port, so the two servers coexist

    # --- Seymour's own server ------------------------------------------------
    host: str = "127.0.0.1"         # again: loopback only
    port: int = 8000                # 8080 is llama-server; we take 8000

    # --- Generation defaults -------------------------------------------------
    max_tokens: int = 2048          # per-reply cap so a runaway generation
                                    # cannot hold a slot forever
    temperature: float = 0.7        # mild creativity; tools may override to 0

    # --- The scheduler (Part IV of the guide) --------------------------------
    # The agent's guaranteed floor: it always holds at least this many slots…
    agent_floor_slots: int = 1
    # …and at least this share of recent decode tokens. If its measured share
    # drops below the floor while it has work queued, its next request is
    # promoted above everything until it recovers.
    agent_floor_share: float = 0.15
    # Tier 2 (deep research etc.) may never occupy every slot — this cap keeps
    # a fan-out job from taking the whole machine.
    tier2_max_slots: int = 2
    # The handshake's probe 4 measures a concurrency speedup ratio. Above this
    # threshold we trust concurrent mode; below it we fall back to serial.
    concurrency_threshold: float = 1.5

    # --- Web search ----------------------------------------------------------
    # A self-hosted SearXNG instance's base URL (e.g. http://localhost:8888).
    # When set, searches go there first (the reference implementation's
    # preferred backend — richer results, no bot walls); DuckDuckGo's HTML
    # endpoint remains the zero-setup fallback either way.
    searxng_url: str = ""
    # Brave Search API key (SEYMOUR_BRAVE_API_KEY): an optional second
    # search provider ahead of the keyless DuckDuckGo fallback. Never
    # reaches a child process — run_command's environment is an allowlist.
    brave_api_key: str = ""

    # --- The agent -----------------------------------------------------------
    # Seconds the agent rests between loop steps when it has an active task.
    # Politeness, not correctness: it keeps the agent from saturating slots
    # the instant they free up, and it gives the UI time to breathe.
    agent_step_pause: float = 2.0
    # On battery power the agent slows down (multiplies the pause) rather than
    # draining the battery at full tilt. The UI says so when it happens.
    agent_battery_pause_factor: float = 5.0

    # --- Derived paths (properties so they always follow the settings) -------
    @property
    def mlx_url(self) -> str:
        """The MLX server's base URL (loopback only, like llama-server)."""
        return f"http://{self.llama_host}:{self.mlx_port}"

    @property
    def llama_url(self) -> str:
        """Base URL of the chat inference server."""
        return f"http://{self.llama_host}:{self.llama_port}"

    @property
    def embed_url(self) -> str:
        """Base URL of the (optional) embedding server."""
        return f"http://{self.llama_host}:{self.embed_port}"

    @property
    def db_path(self) -> Path:
        """The SQLite database file."""
        return self.data_dir / "seymour.db"

    @property
    def soul_path(self) -> Path:
        """The persona ("soul") file — plain Markdown the user can edit."""
        return self.data_dir / "soul.md"

    @property
    def workspace_dir(self) -> Path:
        """The ONLY directory the agent's file tools may touch (its sandbox)."""
        return self.data_dir / "workspace"

    @property
    def uploads_dir(self) -> Path:
        """Where chat attachments (documents, images) are stored."""
        return self.data_dir / "uploads"

    @property
    def gallery_dir(self) -> Path:
        """Where gallery images live (uploaded now; generated someday)."""
        return self.data_dir / "gallery"

    @property
    def static_dir(self) -> Path:
        """The built frontend the browser loads (repo-root /static)."""
        return Path(__file__).resolve().parent.parent / "static"


# One shared instance, imported everywhere as `from seymour.config import settings`.
settings = Settings()

# Create the state directories up front so no other module has to check.
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.workspace_dir.mkdir(parents=True, exist_ok=True)
settings.models_dir.mkdir(parents=True, exist_ok=True)
settings.uploads_dir.mkdir(parents=True, exist_ok=True)
settings.gallery_dir.mkdir(parents=True, exist_ok=True)
