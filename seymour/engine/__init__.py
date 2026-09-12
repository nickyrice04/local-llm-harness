"""The engine layer — the ONLY part of Seymour that knows what an inference
engine is.

    adapter.py    — the abstract interface everything above this layer sees
    llamacpp.py   — the real implementation: launches and speaks to llama-server
    handshake.py  — the four startup probes that MEASURE what the engine can do
    fake.py       — a deterministic in-memory engine, for tests

Two rules keep this seam honest (guide, Chapter 3 §3.7):

1. No model or engine name appears above this layer. If a grep for "qwen" or
   "llama" hits seymour/scheduler/, the abstraction has leaked.
2. Capabilities are measured once by the handshake, then frozen. The scheduler
   never asks "is this the batching kind of model?" — it reads a boolean that
   was established by observation.
"""
