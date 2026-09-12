"""The memory subsystem: what Seymour remembers between conversations.

One system of record — the `memories` table in SQLite — holding both the
text and its embedding vector. (The reference implementation split memory
across a JSON file, a vestigial DB table, and an external vector database;
Seymour deliberately keeps one store you can inspect with `sqlite3`.)

    embedder.py  — turns text into vectors (tiny llama.cpp embedding model
                   when present; a dependency-free hashing embedder otherwise)
    store.py     — add / list / delete / hybrid retrieval (RAG)
    extractor.py — distills durable facts out of conversations, in the
                   background, through the scheduler like everything else
"""

# The store's public functions are the subsystem's public surface.
from seymour.memory.store import add_memory, delete_memory, list_memories, retrieve  # noqa: F401
