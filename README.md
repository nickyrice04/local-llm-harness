# Seymour

A local-first AI workspace for one person on one laptop.
A background agent and a human, sharing one model without taking turns. Designed for higher-efficiency workflow utilizing the power of AI.

Named after Seymour Papert — you understand a system by building it — and, with
three eyes, he helps you *see more*.

![status](https://img.shields.io/badge/runs-entirely_on_your_machine-5b7fc7)

## What it does

- **Chat** with any local GGUF model, streaming, with persistent history.
- **A primary agent** works on long-running tasks in the background — web
  research, file work, anything tool-shaped — *while you keep chatting*.
  It checkpoints constantly, survives restarts, pauses politely on battery,
  and asks before anything irreversible.
- **Deep research** runs multi-round search-read-synthesize pipelines that
  yield to your chat and outrank the agent.
- **Memory**: a reviewable RAG store. Every fact is visible, sourced, and
  deletable; retrieval is hybrid (vectors + keywords + recency).
- **Soul**: Seymour's character is one Markdown file you can edit.
- **Models**: search Hugging Face, download GGUFs with resumable progress,
  and swap brains — every swap re-measures what the model can actually do.
- **The honest avatar**: the pixel Seymour in the corner is driven by real
  scheduler state, never a timer. Typing at his little CRT = working; tea =
  all caught up; zzz = the engine is loading.

## The idea in one paragraph

Most local-AI apps serialize: the background task pauses whenever you chat.
Seymour ships with a known backend — llama.cpp — so it can exploit what that
backend actually provides: parallel slots, continuous batching, a unified KV
pool, and prompt caching by slot continuation. A three-tier scheduler (live
chat > foreground research > background agent, with a guaranteed floor for
the agent) shares ONE copy of the model between you and the agent at the
same time. At startup a **handshake** measures — never assumes — whether
real concurrency is available, and falls back to an honest serial mode when
it isn't. The UI always tells you which mode you're in and why.
See [ARCHITECTURE.md](ARCHITECTURE.md) for the whole map.

## Running it

Prereqs: macOS with `llama-server` on the PATH (`brew install llama.cpp`),
Python 3.11+ with `uv`, and Node for the frontend build.

```bash
# 1. Backend dependencies (once)
uv venv && uv pip install -e ".[dev]"

# 2. Frontend build (once, or `npm run watch` while developing)
npm install && npm run build

# 3. Point Seymour at a model (or skip — the Models tab can download one)
echo 'SEYMOUR_MODEL_PATH=/path/to/your-model.gguf' > .env

# 4. Run
.venv/bin/python -m uvicorn seymour.app:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. First launch shows a short tour. Everything —
model, database, memory, soul — lives on your machine; the only network
traffic is Hugging Face downloads you start and web searches your agent
runs.

Tests (no model needed — the scheduler is proven against a fake engine):

```bash
.venv/bin/python -m pytest
```

## Two backends, one logic

A model is served by whichever engine its files call for: a `.gguf` file
runs on llama.cpp, an MLX directory (`config.json` + `*.safetensors`) runs
on MLX. Both are child processes on loopback with an OpenAI-compatible
API, both go through the same measured handshake and the same scheduler,
and only one runs at a time (two resident 30 GB models collapse
throughput on Apple Silicon). For MLX:

```bash
uv pip install -e ".[mlx]"        # mlx-lm (batching, prompt cache) + mlx-vlm (MTP drafting)
```

Put the model folder in `Models/` (or download it from the Models tab,
which searches Hugging Face in the chosen backend's format). If a sibling
`<name>-MTP-<quant>` drafter folder exists, the MTP setting can switch
the engine to mlx-vlm's speculative decoding — the fastest single reply
this model family gives on this hardware; the default ("auto") keeps
mlx-lm, whose batching is what lets a chat reply start while the agent
is mid-stream. Measured numbers and the reasoning: HARNESS.md and NOTES.md.

## Engine version

Developed against llama.cpp `b10280` (`brew install llama.cpp`). The
startup handshake re-measures slots, per-slot context, prompt caching, and
real concurrency on every launch, so an engine upgrade that silently
changes behaviour is detected and reported rather than quietly degrading
you.

## Layout

```
seymour/     Python backend — one directory per subsystem (see ARCHITECTURE.md)
frontend/    TypeScript sources (esbuild → static/app.js, no framework)
static/      what the browser loads
tests/       deterministic scheduler proofs + support-module tests
guide/       (in the parent repo) the book this build follows
```

## Credit

Seymour stands on other people's work — most of all
[Odysseus](https://github.com/odysseus-dev/odysseus), whose workspace
design this project is a personal fork of in spirit. See
[ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md) for the full accounting.
