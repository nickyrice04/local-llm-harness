"""remember_fact — the one tool that writes to Seymour's long-term memory."""

from seymour.memory.store import add_memory
from seymour.tools import Tool


async def remember_fact(text: str) -> str:
    """Save one durable fact (deduplicated by the store)."""
    text = (text or "").strip()
    if not text:
        return "Error: remember_fact needs the fact as text"
    result = await add_memory(text, kind="fact", source="agent")
    return "Already knew that." if result["duplicate"] else "Remembered."


TOOLS = [
    Tool(
        name="remember_fact",
        description="Save one durable fact about your person or their work to long-term memory.",
        args={"text": "the fact, one short sentence"},
        tier="write",
        func=remember_fact,
        describe=lambda args: "save a fact to memory",
    ),
]
