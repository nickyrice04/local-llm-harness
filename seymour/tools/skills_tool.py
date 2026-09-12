"""use_skill: load one skill's instructions into the conversation.

The catalog shows an INDEX of skills (name — description); this tool
returns the body of one, plus its companion files. Read-only, so chat
never asks permission for it.
"""

from seymour.tools import Tool


async def use_skill(name: str) -> str:
    from seymour import skills
    return skills.render(name)


TOOLS = [
    Tool(
        name="use_skill",
        description=("Load a skill's full instructions (see the Skills list). Call it "
                     "BEFORE starting a task the skill covers, then follow the steps."),
        args={"name": "the skill's name from the Skills list"},
        tier="read", func=use_skill,
    ),
]
