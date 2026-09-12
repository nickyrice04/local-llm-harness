"""The primary agent: Seymour's always-on background worker.

    tools.py   — what the agent can DO: each tool declares an approval tier
    loop.py    — one step of the think → act → observe cycle (kept small;
                 the loop is ~20 lines and the complexity lives in prompt
                 assembly, exactly as the guide's Chapter 4 predicts)
    manager.py — task lifecycle: create, run, pause, block, resume, cancel —
                 and survival across restarts
    power.py   — battery awareness, so a laptop agent is a polite guest

All of the agent's model calls go through the scheduler at Tier 3
(BACKGROUND_AGENT): lowest priority, guaranteed floor. When the scheduler
preempts a step mid-generation the agent checkpoints and retries — that
contract is what makes preemption safe.
"""

# The manager is the subsystem's public face.
from seymour.agent.manager import AgentManager  # noqa: F401
