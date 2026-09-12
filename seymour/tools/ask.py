"""ask_user_question — one question at the right moment.

The harness used to guess when it should ask (dsh's tool-ask-user is
the reference). In a chat run the question is a card in the thread —
the text, optional choices as buttons, a free-text box — and the run
WAITS for the answer, which becomes the tool result. Bounded: an
unanswered question times out with an honest result rather than pinning
the run, and the model is told to proceed on its best assumption.

In the agent loop (unattended, discrete tasks) the loop's own ask_user
is the mechanism (it parks the task as `blocked`); this tool says so
there instead of hanging.
"""

import asyncio
import json

from seymour.tools import context

ANSWER_TIMEOUT_S = 900.0
MAX_OPTIONS = 6


async def ask_user_question(question: str, options: str = "") -> str:
    """Tool entry: ask, wait, return the answer."""
    question = (question or "").strip()
    if not question:
        return "Error: ask_user_question needs the question itself."
    choices: list[str] = []
    raw = (options or "").strip()
    if raw:
        try:
            parsed = json.loads(raw) if raw.startswith("[") else None
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            choices = [str(c).strip() for c in parsed if str(c).strip()][:MAX_OPTIONS]
        else:
            choices = [c.strip() for c in raw.replace("\n", "|").split("|") if c.strip()][:MAX_OPTIONS]
    ask = context.asker()
    if ask is None:
        return ("Error: nobody is watching this run to answer a question. If you are an agent task, "
                "use ask_user (it pauses the task until your person answers); otherwise decide "
                "on your best judgement and say what you assumed.")
    try:
        answer = await asyncio.wait_for(ask({"question": question, "options": choices}), ANSWER_TIMEOUT_S)
    except asyncio.TimeoutError:
        return ("No answer arrived in time. Proceed on your best assumption, and state that assumption "
                "clearly in your reply.")
    answer = (answer or "").strip()
    if not answer:
        return "Your person skipped the question. Proceed on your best assumption and say what you assumed."
    return f"Your person answered: {answer}"


from seymour.tools import Tool                      # noqa: E402  (registry type)

TOOLS = [
    Tool(
        name="ask_user_question",
        description=("Ask your person ONE question and wait for the answer — when a decision is theirs "
                     "to make (which of two designs, whether to delete, what a vague request meant) and "
                     "guessing would waste work. Not for confirmation of obvious steps. Offer options "
                     "when the choices are few."),
        args={"question": "the question, specific and short",
              "options": 'optional choices as a JSON list ["a", "b"] or a | b | c'},
        optional=frozenset({"options"}),
        tier="read", func=ask_user_question,
    ),
]
