"""One step of the agent loop: think → maybe act → observe → checkpoint.

The loop itself is small — the guide's Chapter 4 promise ("the loop is
twenty lines; the complexity is in prompt assembly") holds. What this file
mostly does is build the model's view of the task:

    [system]  soul + working rules + the tool catalog     ← byte-identical
              every step, so llama.cpp's slot-continuation cache means each
              step only prefills the delta, not the whole preamble
    [user]    the task goal                               ← stable per task
    [user]    checkpoint notes + recent journal            ← the changing tail
    [user]    (after a tool ran) its result, guard-wrapped if untrusted

Tool calling is IN-BAND: the model replies with a JSON object and we parse
it. In-band works identically on every GGUF model — native tool-call APIs
vary by chat template — and one visible mechanism beats two half-hidden
ones in a codebase built to be read.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Optional

from seymour import inference, runtime, tools
from seymour.db import AgentStep, AgentTask, SessionLocal
from seymour.engine.adapter import GenerationRequest
from seymour.events import bus
from seymour.guard import untrusted_block
from seymour.persona.soul import get_soul
from seymour.prompts import load
from seymour.scheduler.tiers import Tier

logger = logging.getLogger(__name__)

# How many recent journal entries ride along in the prompt. The durable
# memory of the task is its NOTES (the checkpoint); the journal tail just
# gives the model its immediate context.
JOURNAL_TAIL = 12

# How long one step may take before the loop gives up on it. Generous
# enough for a real think plus a full-size reply (STEP_MIN_TOKENS at the
# measured ~55 tok/s is ~150 s by itself) plus a queue wait on a busy
# machine; short enough that one rabbit-hole doesn't eat a task's whole
# budget. Was 150 s when steps were capped at 2048 tokens.
STEP_DEADLINE_S = 360.0

# The floor on a step's reply cap. Measured 2026-09-02: at 2048 tokens a
# ~300-line HTML file could not be written at all — eleven cut-off
# write_file calls in a row, then the 10-minute task timeout. A step
# gets the saved reply cap or this, whichever is larger; thinking tokens
# count against the same cap, so this must hold a think AND a file.
STEP_MIN_TOKENS = 8192

# Tasks whose thinking has already eaten a whole step budget once. From
# then on their steps START with the hidden channel closed: measured, a
# task holding a long listing exhausted the budget on FOUR consecutive
# steps, paying ~37 s each time before the retry rescued it. A task that
# has shown it rabbit-holes on this context keeps rabbit-holing on it.
# Process-local and bounded by the number of tasks a session runs.
_think_off_tasks: set[str] = set()


@dataclass
class StepOutcome:
    """What one step concluded — the manager acts on this."""

    kind: str                      # "continue" | "done" | "blocked" | "ask"
    text: str = ""                 # report / reason / question
    # The canonical signature of the tool call this step made (tool name +
    # args, key-sorted), or None when the step was a pure thought. The
    # manager compares consecutive signatures to catch a stuck agent —
    # carried here on the outcome because the journal can't answer it:
    # by the time the manager looks, the tool RESULT is the newest entry.
    call_sig: Optional[str] = None


def journal(task_id: str, kind: str, content: str) -> None:
    """Append one entry to the task's journal AND tell the UI.

    The journal is the agent's visible work log: it is how the UI renders
    progress live, how the loop rebuilds context after a restart, and how
    a human audits what actually happened.
    """
    with SessionLocal() as db:
        step = AgentStep(task_id=task_id, kind=kind, content=content[:8000])
        db.add(step)
        db.commit()
        step_id = step.id
    # The event carries the row's id so the UI can dedup: a step that
    # lands live while a journal snapshot is in flight is ALSO in that
    # snapshot (persisted-then-published order), and without the id the
    # merge rendered it twice.
    bus.publish("agent", "step", task_id=task_id, step_id=step_id,
                kind=kind, content=content[:500])   # UI preview, bounded


def _build_messages(task: AgentTask, tail: list[AgentStep],
                    force_answer: bool = False) -> list[dict]:
    """Assemble the model's view of the task (see module docstring).

    `force_answer` builds the CLOSING view instead: no tool catalog at
    all, and an instruction to conclude. A model that keeps reaching for
    tools cannot reach for one that isn't there — which is exactly the
    point (the convergence handshake Odysseus's loop-breaker uses, and
    the fix for tasks that gathered everything they needed and then
    looped instead of saying so).
    """
    if force_answer:
        return [
            {"role": "system", "content": load("agent_system",
                                               soul=get_soul(),
                                               tools="(no tools available)")},
            {"role": "user", "content": f"Your task:\n{task.goal}"},
            {"role": "user", "content": (
                "Your notes so far:\n" + (task.notes or "(none)") + "\n\n"
                "What you did (newest last):\n"
                + ("\n".join(f"[{s.kind}] {s.content}" for s in tail)
                   or "(no steps)")
                + "\n\nNo tools are available now. Answer from what you "
                  "already have: reply with DONE: followed by your result "
                  "(or BLOCKED: and what you still need). Nothing else."),
            },
        ]
    # The stable preamble: soul + rules + tools. Rendered from static
    # templates, so it is byte-identical on every step of every task.
    # The SAME registry the chat executor uses, plus the two loop-owned
    # tools — one catalog, so the agent can do anything a chat can.
    system = load("agent_system", soul=get_soul(),
                  tools=tools.render_catalog("full", loop_tools=True))
    messages = [{"role": "system", "content": system}]
    # The goal, verbatim, as the first user turn.
    messages.append({"role": "user", "content": f"Your task:\n{task.goal}"})
    # The changing tail rides in ONE user message near the end: checkpoint
    # notes first, then the recent journal. Tool results that came from the
    # outside world were guard-wrapped when journaled.
    tail_lines = [f"[{step.kind}] {step.content}" for step in tail]
    messages.append({
        "role": "user",
        "content": (
            "Your notes so far (your own checkpoint — trusted):\n"
            f"{task.notes or '(none yet)'}\n\n"
            "Recent journal (newest last):\n"
            + ("\n".join(tail_lines) or "(no steps yet)")
            + "\n\nDecide your next step now. One tool call, or DONE:/BLOCKED:."
        ),
    })
    return messages


async def run_step(task_id: str, tier: Tier = Tier.BACKGROUND_AGENT,
                   force_answer: bool = False, no_think: bool = False) -> StepOutcome:
    """Execute one full think→act cycle for the task. May raise
    PreemptedError (the manager catches it and retries — that's the
    preemption contract).

    `tier` is who this work runs AS: the primary agent steps at Tier 3
    (background, floor-guaranteed), a discrete chat-started task steps at
    Tier 2 (foreground — the user is loosely waiting on it).
    """
    # Re-read the task fresh each step: pause/cancel and new notes must be
    # visible immediately, and steps can be minutes apart.
    with SessionLocal() as db:
        task = db.get(AgentTask, task_id)
        tail = task.steps[-JOURNAL_TAIL:]
    # A task that has already thought a budget away runs thinking-off.
    no_think = no_think or task_id in _think_off_tasks

    # The saved inference settings drive the sampler here too (same knobs
    # for every run kind), with two agent-specific choices kept: the
    # cooler temperature planning steps want, and thinking's measured
    # default of ON — "auto" resolves to that; `no_think` is the
    # empty-reply fallback below (a step whose whole budget went into
    # hidden reasoning gets ONE retry with the hidden channel closed).
    inf = inference.current()
    sampling = inf.request_kwargs(thinking_default=not no_think)
    sampling["temperature"] = min(inf.temperature, 0.4)
    if no_think:
        sampling["template_kwargs"] = {"enable_thinking": False}
        sampling.pop("reasoning_budget", None)
    request = GenerationRequest(
        messages=_build_messages(task, tail, force_answer),
        max_tokens=max(inf.max_tokens, STEP_MIN_TOKENS),
        **sampling,
        # Thinking stays ON here, and the eval set is why. Turning it off
        # made agent steps FAST and WORSE: multi-step scores fell from
        # 5,5 to 4,3 across runs while tasks started ending "blocked" or
        # "paused" mid-goal. The distinction is real and worth stating:
        # a chat turn is dispatch and synthesis (structured, decided in
        # one hop — thinking off is a clean win there), while an agent
        # step is PLANNING — "given everything so far, what is the next
        # useful action?" — which is exactly the judgement a 35B spends
        # its hidden channel on. Same knob, opposite right answers; that
        # is what a per-run policy is FOR. The cost of thinking (a step
        # that rabbit-holes) is bounded by the step deadline below
        # instead, which is the honest fix for a hang.
        # One cache_key per task: every step of this task lands on the same
        # slot, so the (large, stable) preamble stays cached and each step
        # only prefills its delta. This is the whole §2.7 economy.
        cache_key=f"agent:{task_id}",
    )
    # The step deadline: thinking is allowed, hanging is not. A step that
    # produces nothing in this long is a rabbit-hole, and waiting on it
    # costs the whole task its momentum — the manager journals the
    # timeout and takes the next step instead.
    try:
        reply = await asyncio.wait_for(
            runtime.scheduler.complete(tier, request,
                                       label=f"agent:{task_id[:8]}"),
            timeout=STEP_DEADLINE_S,
        )
    except asyncio.TimeoutError:
        journal(task_id, "error",
                f"Step exceeded {STEP_DEADLINE_S:.0f}s with no reply — "
                "moving on.")
        return StepOutcome("continue")
    reply = reply.strip()

    # ---- Empty reply: the budget went into hidden thinking ----------------
    # Measured 2026-09-02: a task holding an 87-file listing returned NINE
    # empty replies in a row, each ~37 s — at ~55 tok/s that is exactly
    # 2048 tokens of reasoning about how to report, with nothing left for
    # the report. Thinking is right for agent steps (the eval proved it)
    # but not for a step that has already spent one full budget on it:
    # retry THIS step once with the hidden channel closed, so the tokens
    # go to the answer. If even that is empty, journal it and move on
    # (the step budget bounds the loop regardless).
    if not reply:
        if not no_think and not force_answer:
            journal(task_id, "status",
                    "The model spent its whole budget thinking — retrying this "
                    "step with thinking off (and keeping it off for this task).")
            _think_off_tasks.add(task_id)
            return await run_step(task_id, tier, force_answer, no_think=True)
        journal(task_id, "status", "The model returned an empty reply — retrying.")
        return StepOutcome("continue")

    # ---- Terminal declarations first --------------------------------------
    if reply.startswith("DONE:"):
        return StepOutcome("done", reply[len("DONE:"):].strip())
    if reply.startswith("BLOCKED:"):
        return StepOutcome("blocked", reply[len("BLOCKED:"):].strip())

    # A forced closing round has no tools and only one job: end the task.
    # Whatever prose it produced IS the report — treating it as another
    # "continue" would restart the very loop this round exists to break.
    if force_answer:
        return StepOutcome("done", reply)

    # ---- Otherwise: expect a tool call ------------------------------------
    # Any shape a local model produces (our JSON, Hermes, <tool_call>).
    call = tools.parse_call(reply)
    if not call:
        if tools.looks_like_call(reply):
            # It LOOKED like a call and never parsed: the reply cap cut it
            # off mid-content (measured on ~300-line files, where the
            # model then re-issued the same oversized call for ten
            # minutes). Journal the head so the trace shows what it was
            # writing, and hand back a RESULT that names the move that
            # lands: write the file in parts.
            journal(task_id, "thought", reply[:400] + " …[cut off]")
            journal(task_id, "tool_result",
                    f"Your tool call was cut off by the reply cap "
                    f"({max(inf.max_tokens, STEP_MIN_TOKENS)} tokens, thinking "
                    "included) and was NOT executed. Write long files in parts: "
                    "write_file the first part, then append_file the rest — each "
                    "part well under the cap. Keep hidden reasoning short before "
                    "a big write.")
            return StepOutcome("continue")
        # No tool call — a thought. Journal it and carry on; the manager's
        # nudge counter keeps a chatty model from thinking forever.
        journal(task_id, "thought", reply)
        return StepOutcome("continue")

    name = str(call.get("tool"))
    args = call.get("args") or {}
    journal(task_id, "tool_call", json.dumps({"tool": name, "args": args}))
    # The canonical call signature (sort_keys so arg ORDER can't disguise
    # an identical call) — returned on the outcome for repeat detection.
    call_sig = json.dumps({"tool": name, "args": args}, sort_keys=True)

    # The two task-mutating tools live here, not in the registry, because
    # they touch the task row itself:
    if name == "remember_progress":
        # The checkpoint write: whatever the agent noted survives restarts,
        # preemption, and the lid closing.
        note = str(args.get("note", "")).strip()
        with SessionLocal() as db:
            task = db.get(AgentTask, task_id)
            # Append, bounded: notes are a running digest, not a log.
            task.notes = (task.notes + "\n- " + note).strip()[-4000:]
            db.commit()
        journal(task_id, "tool_result", "Progress noted.")
        return StepOutcome("continue", call_sig=call_sig)

    if name == "ask_user":
        # The human-in-the-loop gate: the agent prepares, the person
        # decides. The manager flips the task to 'blocked'.
        question = str(args.get("question", "")).strip()
        if not question:
            # An empty ask strands the task on a question nobody can
            # answer — measured: a model that had finished its work
            # reached for ask_user instead of DONE and blocked itself.
            # Treat it as the malformed call it is: say so and continue.
            # (Repeats trip the stuck detector, which forces a close.)
            journal(task_id, "error",
                    "ask_user needs a real question. If the work is "
                    "done, reply DONE: with your report instead.")
            return StepOutcome("continue", call_sig=call_sig)
        return StepOutcome("ask", question)

    # Registry tools act on the world. The unattended agent is NOT asked
    # before writes or commands: the workspace sandbox (path confinement
    # + sandbox-exec for run_command) is its permission boundary, and
    # anything beyond it must go through ask_user.
    result = await tools.execute(name, args)
    tool = tools.TOOLS.get(name)
    if tool is not None and tool.name in ("web_search", "fetch_page"):
        # Results from the outside world are untrusted — wrap them so the
        # next step's prompt marks them as data, never instructions.
        result = untrusted_block(name, result)
    journal(task_id, "tool_result", result)
    return StepOutcome("continue", call_sig=call_sig)
