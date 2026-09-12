"""Seymour — a local-first AI workspace for one person on one laptop.

A background agent and a human share one model without taking turns.

The package is organized so that each subdirectory owns exactly one concern:

    config.py        — every environment-specific value, in one place
    db.py            — the SQLite schema, as readable SQLAlchemy classes
    events.py        — the in-process event bus that feeds the UI (and avatar)
    engine/          — the ONLY code that knows llama-server exists
    scheduler/       — Seymour's actual contribution: three tiers, two modes
    agent/           — the primary background agent: loop, tools, lifecycle
    memory/          — the RAG memory store: embeddings + retrieval
    persona/         — the "soul" file that gives Seymour a character
    models_manager/  — local model registry + Hugging Face downloads
    routes/          — the HTTP surface, one thin module per feature
    app.py           — the shell that wires all of the above together

Named after Seymour Papert — you understand a system by building it — and,
yes, because a three-eyed assistant helps you see more.
"""

# The package version, importable as `seymour.__version__`.
__version__ = "0.1.0"
