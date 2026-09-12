"""The runtime registry: the few singletons app.py wires at startup.

FastAPI routes and background workers need the live engine, scheduler, and
agent manager. Importing them from each other's modules would create
circular imports; instead app.py builds everything in dependency order and
parks the instances here (setter injection at startup — the same pattern
the reference implementation uses to break its cycles).

Everything starts as None and is populated during the lifespan startup.
Any code that runs before startup finishes (there isn't any, by design)
would fail loudly on the None — which is the correct failure.
"""

# Imported only for type annotations.
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from seymour.agent.manager import AgentManager
    from seymour.engine.adapter import EngineAdapter, EngineCapabilities
    from seymour.engine.profile import ModelProfile
    from seymour.scheduler.core import Scheduler

# The engine adapter (llama.cpp in production, fake in tests).
engine: Optional["EngineAdapter"] = None
# The measured capabilities, frozen at startup by the handshake.
caps: Optional["EngineCapabilities"] = None
# The scheduler every generation goes through.
scheduler: Optional["Scheduler"] = None
# The loaded MODEL's measured profile (engine/profile.py): tools in the
# template, the thinking channel, the system prompt's real token cost.
profile: Optional["ModelProfile"] = None
# The primary agent's manager (task lifecycle + the loop runner).
agent: Optional["AgentManager"] = None
