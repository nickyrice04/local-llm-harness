"""Embedders: turning text into vectors so similar meanings land nearby.

Retrieval-augmented memory needs a way to compare a query against stored
facts by MEANING, not just shared words. That comparison is done on vectors:
embed both texts, take the cosine similarity, higher = more related.

Two implementations, chosen automatically:

    LlamaEmbedder    — a tiny dedicated embedding model (~100 MB .gguf)
                       served by a second llama-server. Real semantic
                       vectors. Used when the file exists at
                       settings.embed_model_path. NOT a second copy of the
                       chat model — a specialist too small to matter for
                       the bandwidth budget.
    HashingEmbedder  — zero dependencies, works offline, instant. Hashes
                       words and word-pairs into a fixed-size vector:
                       cruder (it captures overlapping vocabulary, not
                       true meaning) but honest and never unavailable.

Vectors from different embedders are NOT comparable, so every stored vector
is tagged with its embedder's name and retrieval only compares like with
like. Swapping embedders never corrupts search — old vectors just stop
matching until re-embedded.
"""

# hashlib gives the stable hash for the hashing embedder's buckets.
import hashlib
# math for the vector norm; re for tokenization.
import math
import re
import asyncio
import logging
import subprocess
from typing import Optional, Protocol

import httpx

from seymour.config import settings

logger = logging.getLogger(__name__)


class Embedder(Protocol):
    """What the store needs from any embedder."""

    name: str                                 # tag stored beside each vector

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """One vector per input text."""
        ...


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity: the angle between two vectors, as 0..1-ish.

    1.0 = same direction (very similar), 0 = unrelated. Pure Python is
    plenty here — Seymour holds hundreds of memories, not millions.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class HashingEmbedder:
    """The dependency-free fallback: hashed bag-of-words + word pairs.

    How it works, in three steps:
      1. Tokenize into lowercase words; also form adjacent word PAIRS
         (bigrams), so "new york" and "york new" differ.
      2. Hash each token to one of `dims` buckets and count hits — a
         fixed-size fingerprint of the text's vocabulary.
      3. L2-normalize, so cosine similarity compares proportions rather
         than lengths.

    It cannot know that "car" ≈ "automobile" — that is what the real
    embedding model buys — but shared-vocabulary matching is a perfectly
    useful floor, and it can never fail, hang, or need a download.
    """

    name = "hashing"

    def __init__(self, dims: int = 512) -> None:
        self._dims = dims                     # vector width (buckets)

    def _tokens(self, text: str) -> list[str]:
        """Lowercase words, plus adjacent-pair tokens."""
        words = re.findall(r"[a-z0-9]+", text.lower())
        pairs = [f"{a}_{b}" for a, b in zip(words, words[1:])]
        return words + pairs

    def _bucket(self, token: str) -> int:
        """Stable token → bucket index (md5, NOT Python's hash(), which is
        randomized per process and would scramble stored vectors)."""
        return int(hashlib.md5(token.encode()).hexdigest(), 16) % self._dims

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Count hashed tokens into buckets, then normalize."""
        vectors = []
        for text in texts:
            v = [0.0] * self._dims
            for token in self._tokens(text):
                v[self._bucket(token)] += 1.0
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            vectors.append([x / norm for x in v])
        return vectors


class LlamaEmbedder:
    """A dedicated embedding model behind a second, tiny llama-server.

    Mirrors the chat engine's supervision pattern at a fraction of the
    size: launch as a child process, poll /health, POST /v1/embeddings,
    SIGTERM on exit. Any failure degrades to raising, and the store's
    caller falls back to the hashing embedder — memory must never block
    the app (a rule inherited from the reference implementation).
    """

    name = "llama"

    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen] = None
        self._client: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        """Launch the embedding server and wait for readiness."""
        cmd = [
            "llama-server",
            "-m", str(settings.embed_model_path),  # the tiny embedding model
            "--host", settings.llama_host,         # loopback only, as always
            "--port", str(settings.embed_port),
            "--embedding",                         # embeddings mode, not chat
        ]
        logger.info("starting embedding server: %s", " ".join(cmd))
        self._process = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self._client = httpx.AsyncClient(base_url=settings.embed_url, timeout=60.0)
        # Small model → short readiness budget, same dead-child discipline.
        deadline = asyncio.get_running_loop().time() + 60.0
        while asyncio.get_running_loop().time() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError("embedding server exited during startup")
            try:
                r = await self._client.get("/health", timeout=2.0)
                if r.status_code == 200:
                    return
            except httpx.RequestError:
                pass
            await asyncio.sleep(0.5)
        raise TimeoutError("embedding server not ready after 60s")

    async def stop(self) -> None:
        """Shut the embedding server down (mirror of start)."""
        if self._client:
            await self._client.aclose()
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """POST /v1/embeddings — the OpenAI-compatible embeddings shape."""
        response = await self._client.post(
            "/v1/embeddings", json={"input": texts, "model": "embedding"}
        )
        response.raise_for_status()
        body = response.json()
        # Results arrive with an index field; sort to guarantee input order.
        rows = sorted(body.get("data", []), key=lambda d: d.get("index", 0))
        return [row["embedding"] for row in rows]


# ---------------------------------------------------------------- selection
# The active embedder, decided once at startup by app.py:
#   embed model file present → LlamaEmbedder (started/stopped in lifespan)
#   otherwise               → HashingEmbedder (always works)
# Modules import this holder and call .embed() without caring which is live.
active: Embedder = HashingEmbedder()


def use(embedder: Embedder) -> None:
    """Swap the active embedder (called from app startup)."""
    global active
    active = embedder
    logger.info("memory embedder: %s", embedder.name)
