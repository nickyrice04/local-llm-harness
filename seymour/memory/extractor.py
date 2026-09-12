"""Background fact extraction: how memories appear without anyone asking.

After a chat exchange finishes, this runs quietly — as a Tier 3 request,
because nobody is waiting on it — and asks the model whether the recent
conversation contained anything durable about the user. At most two facts
per pass, deduplicated by the store.

Hard-won details inherited from the reference implementation:
- The transcript is flattened into ONE user message. Sending raw
  alternating roles makes the model continue the conversation instead of
  analyzing it (their controlled repro: 0/6 extractions vs 6/6 flattened).
- max_tokens is generous (reasoning models think before emitting JSON;
  a tight budget truncates to unparseable output that reads as "no facts").
- Parsing goes through llm_json.parse_json_array, which tolerates think
  blocks, fences, echoed examples, and truncation.
"""

import logging

from seymour import runtime
from seymour.engine.adapter import GenerationRequest
from seymour.llm_json import parse_json_array
from seymour.memory.store import add_memory
from seymour.prompts import load
from seymour.scheduler.tiers import PreemptedError, Tier

logger = logging.getLogger(__name__)

# How much of the tail of the conversation the extractor reads.
CONTEXT_WINDOW = 6
# The cap on facts per pass — extraction should trickle, not flood.
MAX_FACTS = 2


async def extract_from_messages(messages: list[dict]) -> int:
    """Distill durable facts from a conversation tail. Returns count added.

    Called fire-and-forget after a chat reply completes. Every failure
    path returns 0 — extraction is a bonus, never a blocker.
    """
    # Flatten the last few turns into one labeled transcript (see module
    # docstring for why one message instead of role-play format).
    tail = messages[-CONTEXT_WINDOW:]
    transcript = "\n".join(f"{m['role']}: {m['content']}" for m in tail)

    request = GenerationRequest(
        messages=[{
            "role": "user",
            "content": load("memory_extract",
                            transcript=transcript, max_facts=str(MAX_FACTS)),
        }],
        max_tokens=4096,          # generous: think-tokens come before JSON
        temperature=0.1,          # extraction wants precision, not flair
    )
    try:
        # Tier 3: extraction is background work and must never slow the
        # chat that triggered it.
        reply = await runtime.scheduler.complete(
            Tier.BACKGROUND_AGENT, request, label="memory:extract"
        )
    except PreemptedError:
        # The scheduler bumped us for real work — fine, we just skip this
        # pass rather than re-queue (the next exchange re-triggers it).
        return 0
    except Exception:
        logger.exception("memory extraction failed")
        return 0

    added = 0
    # Parse defensively and store each fact through the dedup gate.
    for item in parse_json_array(reply)[:MAX_FACTS]:
        if not isinstance(item, dict) or not item.get("text"):
            continue
        # The store validates the category (unknown → "fact") and
        # auto-pins identity/contact facts.
        kind = str(item.get("category", "fact"))
        result = await add_memory(str(item["text"]), kind=kind, source="chat")
        if not result.get("duplicate"):
            added += 1
    if added:
        logger.info("memory extraction: %d new fact(s)", added)
    return added
