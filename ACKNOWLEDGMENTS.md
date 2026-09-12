# Acknowledgments

Seymour is mostly other people's good ideas with one new part bolted on
(the scheduler). This file keeps the accounting honest.

## License

Seymour is licensed **AGPL-3.0-or-later** (see LICENSE) — a deliberate
choice, made 2026-08-18: the project adapts patterns and fragments from
Odysseus, which is AGPL-3.0-or-later, and carrying the same license keeps
that inheritance clean. Reference material from MIT projects
(deepseek-harness, oh-my-pi) is compatible and attributed below when used.

## Odysseus

[Odysseus](https://github.com/odysseus-dev/odysseus) (AGPL-3.0-or-later) is
the reference implementation this project forks in spirit: a self-hosted AI
workspace whose breadth — chat, tasks, memory, personas, deep research —
defined what Seymour should feel like. Where Odysseus must run on any
backend and therefore serializes model access (foreground cancels
background), Seymour ships with llama.cpp and schedules instead. That is
the one deliberate difference; nearly everything else is inheritance:

| Idea in Seymour | Where it came from |
|---|---|
| Serial mode's semantics (foreground preempts, background yields) | Odysseus's contention gate (`src/llm_core.py`) |
| Slot affinity via a stable per-conversation key + `cache_prompt` | Odysseus's fix for prompt-cache thrashing (`_apply_local_cache_affinity`) |
| Reading capabilities from `/props` + `/slots`, serving context ≠ training context | `src/model_capability_readers/llamacpp.py` |
| Save the user's turn before streaming; persist partials in `finally` | Odysseus's chat routes |
| Untrusted-content guard blocks with escaped markers | `src/prompt_security.py` |
| KV-cache-friendly prompt assembly (stable system prompt, dynamic content near the end) | Odysseus issue #2927 and its fix |
| Memory: budgeted, relevance-gated hybrid retrieval (vector + keyword + recency) | Odysseus's memory scoring |
| Extraction: flattened transcripts, generous token budgets, tolerant JSON parsing | Odysseus's memory extractor's battle scars |
| Fresh-context completion verifier, failing open | `_run_verifier_subagent` |
| Loop-breakers: repeat detection, force-answer nudges, rounds-exhausted | Odysseus's agent loop |
| HF downloads into the hub cache layout (never `--local-dir`) for resume | Odysseus issue #2722 |
| Deep research's stage machine (plan → search → extract → synthesize → decide) and keep-partial-work rule | `src/deep_research.py` |
| Literal `edit_file` semantics (unique match, errors that say why), secret-path denylists in `ls`/`glob`/`grep` | `src/agent_tools/filesystem_tools.py`, `src/tool_execution.py` |
| DNS-pinned fetch transport (connect to the checked address), capped streaming GET with identity encoding, PDF/text/HTML routing | `services/search/content.py` |
| Multi-provider search chain with freshness filters, DuckDuckGo redirect unwrapping | `services/search/providers.py` |
| Process-group kills and streaming readers for subprocess tools | `src/agent_tools/subprocess_tools.py` |

No Odysseus code was copied verbatim; the patterns above were studied and
reimplemented for this codebase's scale. If any excerpt is ever lifted
directly, the file will carry an attribution header and this table a row
saying so, and the AGPL terms apply.

## oh-my-pi (omp)

[oh-my-pi](https://github.com/oh-my-pi/oh-my-pi) (a fork of Mario
Zechner's pi-mono), a coding-agent harness whose engineering discipline
shaped Seymour's agent layer:

- tools declare approval tiers (read / write / exec); Seymour's exec
  tier arrived with the harness rebuild, confined by macOS sandbox-exec;
- prompts live in static files, never string-built in code;
- every "the model might never stop" path gets a bounded counter with its
  reset condition documented;
- **hashline** (`packages/hashline`): line-addressed edits anchored on a
  content tag from the latest read, with a seen-lines guard and a fresh
  tag on every result. Seymour's `edit_lines` is a JSON-native subset of
  that idea (ranges + tag + guard; no block ops or registers), and
  `read_file`/`grep` print the `[path#TAG]` + `N:text` shape it needs;
- the Qwen3 tool-call dialect notes (`docs/toolconv/qwen3.md`), which is
  why `parse_call` accepts `<tool_call>` blocks and stringified arguments;
- the read/grep/glob caps and exact-next-action footers ("Use offset=N
  to continue").

## deepseek-harness (dsh)

[deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) (MIT)
is the structural model for Seymour's run executor and tool pipeline:

- the append-only event log as the single source of truth ("model-visible
  means logged"), which is what the Runs trace tab reads;
- tool failures as *results* the model reads, never exceptions that end
  a turn; unknown tools name the real ones;
- the bash executor's semantics: exit code, timeout and kill reported
  independently; a nonzero exit is data, not a tool error; head + tail
  in the prompt with the full output spilled to a file;
- the web seam's shape (search and fetch as two operations under one
  policy owner; a non-2xx status is a result, not an error) — with the
  private-network blocking dsh defers added here, because a LAN laptop
  cannot defer it;
- the repeat-tool reminder (advisory nudges at 3/5/8 identical calls);
- the once-per-run approval that fails closed on no answer;
- the MCP client's shape (`packages/mcp/mcp-client`): stdio transport,
  tools registered under `mcp__<server>__<tool>`, per-call timeout,
  tools-only scope.

## MLX (mlx-lm, mlx-vlm)

[mlx-lm](https://github.com/ml-explore/mlx-lm) and
[mlx-vlm](https://github.com/Blaizzy/mlx-vlm) (both MIT) are the second
mechanism layer, for Apple Silicon: mlx-lm's server brings continuous
batching and a prefix-matching prompt cache; mlx-vlm's brings MTP
speculative decoding with a drafter checkpoint. Seymour launches one or
the other as a child and measures it with the same handshake it runs on
llama-server. The mlx-community conversions of Qwen3.8-27B and its MTP
drafter are what made the port worth doing. Two pointers Nick sent set the
expectations the benchmark then checked: bluehawana's Apple-silicon
concurrency dataset for Qwen3.8-27B and jordanilchev's local-qwen
setup (DFlash/DDTree speculative decoding on M4).

## Agent Skills

The skills format (SKILL.md with a name/description front matter, the
body loaded on demand) follows the Agent Skills convention as Claude
Code, oh-my-pi and deepseek-harness use it, so a skill written for any
of them drops into `~/.seymour/skills` unchanged.

## The agency layer (2026-09-11)

The overhaul's tools and economy re-express ideas from the same three
harnesses, credited here by the package each came from:

- **deepseek-harness**: `packages/spill` (oversized results to a file,
  head/tail excerpt with a locator), `packages/compaction` (the tool-
  pairing balance check — a call is never split from its result),
  `packages/todo` (the plan kept by the harness), `packages/jobs`
  (background jobs with consuming reads), `tool-bash-persistent` (one
  shell per session), `tool-fs-search` (glob), `tool-ask-user`, and its
  terminal / diff / search cards in the chat.
- **oh-my-pi**: the pruning ruleset in `packages/coding-agent` (elide
  uneventful results, blank superseded reads), the `task` batch contract
  (`# Target / # Change / # Acceptance`, a bounded summary plus the
  full artifact), and the git tools. Its `snapcompact` was read and
  deliberately not followed.
- **Odysseus**: the guarded subprocess pattern the persistent shell and
  jobs still run under.

## llama.cpp

[llama.cpp](https://github.com/ggml-org/llama.cpp) provides the entire
mechanism layer: slots, continuous batching, the unified KV pool, prompt
caching by slot continuation, and the `/props`–`/slots` introspection the
handshake is built on. Seymour is a policy layer standing on that
mechanism.

## Research literature

The scheduling design follows the finding (AGENTSERVESIM, arXiv
2606.09613; also Autellix, InferCept, Continuum) that **prefix caching,
not scheduling policy, is the first-order win for agentic workloads** —
which is why slot affinity and cache-stable prompts came before any
scheduler tuning.

## The model authors

Qwen3.6-35B-A3B (Apache-2.0) is the development model: a hybrid-attention
MoE whose 2 KV heads and ~3B active parameters are what make two-tier
concurrency affordable on a laptop at all.

## Seymour Papert

Who taught that you understand a system by building it. The avatar that
never lies about the machine's state is the Logo turtle's great-grandchild.
