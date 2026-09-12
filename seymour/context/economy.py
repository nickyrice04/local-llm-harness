"""The Economy: one object per run that keeps the prompt affordable.

It owns three decisions and reports every one of them:

    spill    per result, at append time (spill.py)
    prune    when the prompt passes PRUNE_AT of the context (prune.py)
    compact  when it passes COMPACT_AT even after pruning (compact.py)

The run's messages carry a `_meta` dict (kind, tool, path, ok, pinned…)
that the executor sets through `tag`/`pair` and that `strip` removes
before the engine sees them. Sizes are ESTIMATED at 4 characters per
token — the same rule inference.budget_history uses — because the
tokenizer is on the other side of an HTTP boundary and the estimate is
within ~15 % on English and code, which is plenty for a threshold.

Why the thresholds are where they are: pruning changes old messages,
which invalidates llama-server's prompt cache from the first changed
byte onward — a re-prefill of everything after it. While the prompt is
under 40 % of the context that cache is worth more than the tokens it
holds; above it, the tokens are. Compaction rewrites the prefix outright
(one full prefill) so it waits for 70 %, and it keeps the last K pairs
verbatim so the model's immediate working set is never a paraphrase.
"""

import logging
from typing import Awaitable, Callable

from seymour.context import compact as _compact
from seymour.context import prune as _prune
from seymour.context import spill as _spill

logger = logging.getLogger(__name__)

# Characters per token, the estimate used everywhere in Seymour.
CHARS_PER_TOKEN = 4
# Fractions of the loaded context at which each mechanism fires.
PRUNE_AT = 0.40
COMPACT_AT = 0.70
# How many recent call/result pairs stay verbatim through pruning AND
# compaction: the model's working set.
KEEP_RECENT_PAIRS = 3
# When no engine capabilities are known (tests, an adopted server that
# reported nothing), assume this context — llama-server's default here.
FALLBACK_CONTEXT_TOKENS = 32_768


def strip(convo: list[dict]) -> list[dict]:
    """The messages as the engine must see them: no `_meta`."""
    return [{k: v for k, v in m.items() if k != "_meta"} for m in convo]


def _chars(message: dict) -> int:
    content = message.get("content")
    if isinstance(content, list):
        # Image parts are counted by a fixed allowance (a vision model
        # spends a few hundred tokens per image; the base64 is not text).
        return sum(len(p.get("text", "")) if p.get("type") == "text" else 1_200
                   for p in content if isinstance(p, dict))
    return len(str(content or ""))


def estimate_tokens(convo: list[dict]) -> int:
    """The prompt's size in tokens, estimated (see module docstring)."""
    return sum(_chars(m) for m in convo) // CHARS_PER_TOKEN + 4 * len(convo)


class Economy:
    """The per-run context economy. Construct one per run; call
    `prepare` before every model call; read `stats` at the end."""

    def __init__(self, *, context_tokens: int | None, reply_tokens: int,
                 summarize: Callable[[list[dict]], Awaitable[str]] | None,
                 on_event: Callable[..., None] | None = None, run_id: str = "",
                 keep_recent_pairs: int = KEEP_RECENT_PAIRS,
                 prune_at: float = PRUNE_AT, compact_at: float = COMPACT_AT,
                 inline_chars: int = _spill.INLINE_CHARS) -> None:
        self.context_tokens = int(context_tokens or FALLBACK_CONTEXT_TOKENS)
        self.reply_tokens = int(reply_tokens)
        self._summarize = summarize
        self._on_event = on_event or (lambda *a, **k: None)
        self.run_id = run_id
        self.keep = keep_recent_pairs
        self.prune_at, self.compact_at = prune_at, compact_at
        self.inline_chars = inline_chars
        self.request_text = ""
        # The running tally the run_end event reports.
        self.stats = {"spills": 0, "spilled_chars": 0, "prunes": 0, "pruned_chars": 0,
                      "compactions": 0, "compacted_messages": 0, "peak_tokens": 0}

    # ------------------------------------------------------------- tagging
    @staticmethod
    def tag(message: dict, **meta) -> dict:
        """A copy of `message` carrying `_meta` (merged over any existing)."""
        return {**message, "_meta": {**(message.get("_meta") or {}), **meta}}

    def tag_base(self, base_messages: list[dict]) -> list[dict]:
        """Tag the route's assembled prompt: system first, the last user
        message is the pinned request, everything between is history or
        per-turn context."""
        out: list[dict] = []
        last = len(base_messages) - 1
        for i, message in enumerate(base_messages):
            if i == 0 and message.get("role") == "system":
                out.append(self.tag(message, kind="system"))
            elif i == last and message.get("role") == "user":
                out.append(self.tag(message, kind="request", pinned=True))
                self.request_text = _compact._text(message)[:4000]
            else:
                out.append(self.tag(message, kind="history"))
        return out

    def result_text(self, name: str, result: str) -> tuple[str, dict | None]:
        """Apply the spill policy to a fresh result; log a spill event."""
        text, info = _spill.apply(name, result, run_id=self.run_id, inline=self.inline_chars)
        if info:
            self.stats["spills"] += 1
            self.stats["spilled_chars"] += info["chars"]
            self._on_event("context", what="spill", tool=name, path=info["path"],
                           chars=info["chars"], inline_chars=info["inline_chars"], lines=info["lines"])
        return text, info

    def pair(self, call_text: str, name: str, args: dict, result_message_content: str,
             ok: bool = True, spill: dict | None = None) -> list[dict]:
        """The two tagged messages for one executed tool call."""
        path = str((args or {}).get("path") or "") or None
        return [
            {"role": "assistant", "content": call_text,
             "_meta": {"kind": "call", "tool": name, "path": path, "args": args}},
            {"role": "user", "content": result_message_content,
             "_meta": {"kind": "result", "tool": name, "path": path, "ok": ok,
                       "spill": (spill or {}).get("path")}},
        ]

    # ------------------------------------------------------------ deciding
    def _budget(self) -> int:
        """Tokens the prompt may use: the context less the reply's room."""
        return max(self.context_tokens - self.reply_tokens, 1_024)

    async def prepare(self, convo: list[dict]) -> list[dict]:
        """Prune and compact as pressure requires; returns the convo to
        send (still tagged — call `strip` for the engine)."""
        tokens = estimate_tokens(convo)
        self.stats["peak_tokens"] = max(self.stats["peak_tokens"], tokens)
        budget = self._budget()
        if tokens < budget * self.prune_at:
            return convo
        # ---- prune ---------------------------------------------------------
        pruned, actions = _prune.prune(convo, self.keep)
        if actions:
            after = estimate_tokens(pruned)
            self.stats["prunes"] += len(actions)
            self.stats["pruned_chars"] += sum(a["saved"] for a in actions)
            self._on_event("context", what="prune", before_tokens=tokens, after_tokens=after,
                           budget_tokens=budget, actions=actions[:40], count=len(actions))
            convo, tokens = pruned, after
        if tokens < budget * self.compact_at:
            return convo
        # ---- compact -------------------------------------------------------
        chosen = _compact.choose_range(convo, self.keep)
        if chosen is None:
            self._on_event("context", what="compact skipped", reason="nothing old enough to summarize",
                           tokens=tokens, budget_tokens=budget)
            return convo
        start, end = chosen
        method = "model"
        summary = ""
        if self._summarize is not None:
            try:
                summary = (await self._summarize(
                    _compact.summary_prompt(convo, start, end, self.request_text))).strip()
            except Exception as error:                    # a summary must never end a run
                logger.warning("compaction summary failed: %s", error)
                self._on_event("context", what="compact summary failed", error=str(error)[:300])
        if not summary or len(summary) < 40:
            method = "mechanical"
            summary = _compact.mechanical_summary(convo, start, end)
        new = _compact.splice(convo, start, end, summary, method)
        after = estimate_tokens(new)
        self.stats["compactions"] += 1
        self.stats["compacted_messages"] += end - start
        self._on_event("context", what="compact", method=method, replaced_messages=end - start,
                       before_tokens=tokens, after_tokens=after, budget_tokens=budget,
                       kept_recent_pairs=self.keep, summary=summary)
        return new
