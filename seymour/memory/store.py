"""The memory store: add, list, delete, and — the interesting part — retrieve.

Retrieval is hybrid, scored as (weights adapted from the reference
implementation's tuning):

    score = 0.55 · vector similarity      "does it MEAN the same thing?"
          + 0.40 · keyword overlap        "does it SHARE actual words?"
          + 0.05 · recency                "newer beats older on a tie"

with two gates so junk never reaches the prompt: an entry needs a minimum
signal on vector OR keyword to be considered at all, and a minimum combined
score to be returned. At most `k` memories are ever injected — memory is a
seasoning, not the meal.
"""

# json to (de)serialize embedding vectors; datetime for recency scoring.
import json
import logging
import re
from datetime import timezone

from seymour.db import MemoryEntry, SessionLocal, utcnow
from seymour.events import bus
from seymour.memory import embedder as emb

logger = logging.getLogger(__name__)

# ---- Scoring knobs (module constants: tuning values, not user settings) ----
VECTOR_WEIGHT = 0.55        # semantic similarity's share of the score
KEYWORD_WEIGHT = 0.40       # literal word overlap's share
RECENCY_WEIGHT = 0.05       # freshness tiebreaker's share
MIN_VECTOR_SIGNAL = 0.20    # gate: vector sim below this AND …
MIN_KEYWORD_SIGNAL = 0.08   # … keyword overlap below this → skip entry
MIN_FINAL_SCORE = 0.12      # gate: combined score below this → not returned
DEDUP_THRESHOLD = 0.92      # add(): similarity above this = duplicate
MAX_INJECTED = 5            # never inject more than this many memories

# The category vocabulary (the Odysseus set — see ACKNOWLEDGMENTS.md).
# Categories drive retrieval boosts and the UI's chips; anything else
# degrades to "fact" instead of polluting the store.
CATEGORIES = ("fact", "identity", "preference", "contact", "project", "goal")
# CORE categories are who-the-user-IS facts: when pinned, they ride in
# EVERY chat (capped), not just relevant ones.
CORE_CATEGORIES = ("identity", "contact")
# How many pinned core memories may ride along unconditionally.
MAX_PINNED = 5

# Query-intent boosts: when the question is ABOUT a category, matching
# memories score higher (a compact port of the reference implementation's
# category-boost table).
_INTENT_BOOSTS: list[tuple[tuple[str, ...], str, float]] = [
    (("name", "who am i", "call me"), "identity", 1.4),
    (("phone", "email", "address", "contact"), "contact", 1.3),
    (("like", "prefer", "favorite"), "preference", 1.2),
]


def _keyword_overlap(query: str, text: str) -> float:
    """Fraction of the query's words that appear in the text (0..1)."""
    query_words = set(re.findall(r"[a-z0-9]+", query.lower()))
    if not query_words:
        return 0.0
    text_words = set(re.findall(r"[a-z0-9]+", text.lower()))
    return len(query_words & text_words) / len(query_words)


def _recency(entry: MemoryEntry) -> float:
    """1.0 for brand new, decaying toward 0 over months."""
    created = entry.created_at
    # SQLite hands back naive datetimes; re-attach UTC before comparing.
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (utcnow() - created).total_seconds() / 86400)
    return 1.0 / (1.0 + age_days * 0.05)


async def add_memory(content: str, kind: str = "fact", source: str = "user") -> dict:
    """Store one fact — unless we already know it.

    Dedup-by-similarity: if an existing memory from the SAME embedder is
    nearly identical (cosine > threshold), return it instead of inserting
    a twin. The comparison never crosses embedders (their vectors don't
    share a space).
    """
    content = content.strip()
    # Unknown categories degrade to "fact" — one vocabulary, everywhere.
    if kind not in CATEGORIES:
        kind = "fact"
    # Embed first; on any embedder failure store with an empty vector —
    # a fact without a vector still works via keyword retrieval. Memory
    # must degrade, never block (reference implementation's rule).
    try:
        vector = (await emb.active.embed([content]))[0]
        embedder_name = emb.active.name
    except Exception:
        logger.exception("embedding failed; storing memory keyword-only")
        vector, embedder_name = [], "none"

    with SessionLocal() as db:
        for existing in db.query(MemoryEntry).all():
            # Cheap exact-duplicate check first.
            if existing.content.strip().lower() == content.lower():
                return {"id": existing.id, "duplicate": True}
            # Then the vector check, same-embedder only.
            if existing.embedder == embedder_name and vector:
                if emb.cosine(vector, json.loads(existing.embedding)) > DEDUP_THRESHOLD:
                    return {"id": existing.id, "duplicate": True}
        entry = MemoryEntry(
            content=content,
            kind=kind,
            source=source,
            embedding=json.dumps(vector),
            embedder=embedder_name,
            # Who-the-user-IS facts auto-pin (the Odysseus rule): a name
            # or contact detail should never depend on retrieval luck.
            pinned=kind in CORE_CATEGORIES,
        )
        db.add(entry)
        db.commit()
        # Tell the UI so an open Memory tab updates live.
        bus.publish("memory", "added", id=entry.id, content=content,
                    kind=kind, source=source)
        return {"id": entry.id, "duplicate": False}


def list_memories() -> list[dict]:
    """Every memory, pinned first then newest — the reviewable store."""
    with SessionLocal() as db:
        entries = (db.query(MemoryEntry)
                   .order_by(MemoryEntry.pinned.desc(),
                             MemoryEntry.created_at.desc()).all())
        return [
            {
                "id": e.id, "content": e.content, "kind": e.kind,
                "source": e.source, "pinned": e.pinned, "uses": e.uses,
                "created_at": e.created_at.isoformat(),
            }
            for e in entries
        ]


def set_pinned(memory_id: int, pinned: bool) -> bool:
    """Pin or unpin one memory. Pinned core facts (identity/contact) ride
    in every chat; other pins simply win ties in retrieval."""
    with SessionLocal() as db:
        entry = db.get(MemoryEntry, memory_id)
        if entry is None:
            return False
        entry.pinned = pinned
        db.commit()
        bus.publish("memory", "pinned", id=memory_id, pinned=pinned)
        return True


def increment_uses(memory_ids: list[int]) -> None:
    """Count an INJECTION (not a mere retrieval) for each memory — the
    UI's '12×' badge, and honest evidence of which facts actually help."""
    if not memory_ids:
        return
    with SessionLocal() as db:
        for entry in db.query(MemoryEntry).filter(MemoryEntry.id.in_(memory_ids)):
            entry.uses = (entry.uses or 0) + 1
        db.commit()


def delete_memory(memory_id: int) -> bool:
    """Forget on demand — the user owns this store."""
    with SessionLocal() as db:
        entry = db.get(MemoryEntry, memory_id)
        if entry is None:
            return False
        db.delete(entry)
        db.commit()
        bus.publish("memory", "deleted", id=memory_id)
        return True


async def retrieve(query: str, k: int = MAX_INJECTED) -> list[dict]:
    """The RAG step: pinned core facts + the most relevant memories.

    Two lanes (the Odysseus design, compacted):

      1. PINNED CORE — pinned identity/contact facts ride along in every
         chat (capped at MAX_PINNED, newest first). A name should never
         depend on retrieval luck.
      2. SCORED — everything else competes on the hybrid score, with a
         category boost when the query's intent matches ("what's my
         name?" boosts identity memories).

    Brute-force over every row — deliberately. At Seymour's scale
    (hundreds of memories) a scan is instant, and keeping it visible makes
    the whole retrieval mechanism readable in one function. Swap in an
    index when the scan shows up in a profile, not before.
    """
    # Embed the query once; on failure fall back to keyword-only scoring.
    try:
        query_vector = (await emb.active.embed([query]))[0]
        embedder_name = emb.active.name
    except Exception:
        logger.exception("query embedding failed; keyword-only retrieval")
        query_vector, embedder_name = [], "none"

    # Which category (if any) does the QUESTION's wording point at?
    lowered = query.lower()
    boost_kind, boost = "", 1.0
    for words, category, factor in _INTENT_BOOSTS:
        if any(w in lowered for w in words):
            boost_kind, boost = category, factor
            break

    pinned_core: list[MemoryEntry] = []
    scored: list[tuple[float, MemoryEntry]] = []
    with SessionLocal() as db:
        for entry in db.query(MemoryEntry).all():
            # Lane 1: pinned core facts skip the scoring entirely.
            if entry.pinned and entry.kind in CORE_CATEGORIES:
                pinned_core.append(entry)
                continue
            # Vector similarity — only meaningful within one embedder.
            if query_vector and entry.embedder == embedder_name:
                vec = emb.cosine(query_vector, json.loads(entry.embedding))
            else:
                vec = 0.0
            # Literal word overlap.
            kw = _keyword_overlap(query, entry.content)
            # Gate 1: no meaningful signal on either axis → skip.
            if vec < MIN_VECTOR_SIGNAL and kw < MIN_KEYWORD_SIGNAL:
                continue
            # The hybrid score, intent-boosted when categories match, and
            # nudged for non-core pins (a pin is the user saying "this
            # matters" — it wins ties, not the whole race).
            score = (VECTOR_WEIGHT * vec
                     + KEYWORD_WEIGHT * kw
                     + RECENCY_WEIGHT * _recency(entry))
            if entry.kind == boost_kind:
                score *= boost
            if entry.pinned:
                score *= 1.15
            # Gate 2: combined score still too weak → skip.
            if score < MIN_FINAL_SCORE:
                continue
            scored.append((score, entry))

    # Assemble: core pins first (newest first, capped), then the scored
    # winners in the remaining slots.
    pinned_core.sort(key=lambda e: e.created_at, reverse=True)
    picked = pinned_core[:MAX_PINNED]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    slots = max(0, k - len(picked))
    picked += [e for _, e in scored[:slots]]
    return [
        {"id": e.id, "content": e.content, "kind": e.kind,
         "pinned": e.pinned, "score": 1.0 if e.pinned and e.kind in CORE_CATEGORIES
         else round(next((s for s, x in scored if x.id == e.id), 0.0), 3)}
        for e in picked
    ]


def format_for_prompt(memories: list[dict]) -> str:
    """Render retrieved memories as the text that goes inside the guard
    block (see guard.py — memories are retrieved content, hence untrusted
    placement even though the user typed most of them)."""
    return "\n".join(f"- {m['content']}" for m in memories)
