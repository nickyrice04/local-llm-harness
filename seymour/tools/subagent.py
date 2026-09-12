"""task / tasks — subagents: a child run with a blank context.

How parallel research and multi-file changes scale past one context
window (oh-my-pi's `task` batch, dsh's `packages/subagent`): the parent
writes a CONTRACT —

    # Target      what to work on (files, a question, a page)
    # Change      what to do or find out
    # Acceptance  how the child knows it is done (tests green, a list of
                  facts with sources, a file that passes check_page)

— and a child run starts with nothing but that contract, the same
tools and the same sandbox, at Tier 2 (a foreground task: it outranks
the background agent, yields to live chat). It runs to DONE, BLOCKED or
its step budget; the parent gets a BOUNDED summary plus the path of the
full artifact (the child's whole journal, written to the workspace) —
its context stays small whatever the child read.

`tasks` runs several contracts at once (the discrete pool's concurrency
cap is the brake) and returns every summary together. A child may not
spawn children (depth 1): the recursion is bounded by construction.
"""

import asyncio
import json
import time

from seymour.tools import context, paths

MAX_SUMMARY_CHARS = 4_000
DEFAULT_TIMEOUT_S = 900
MAX_TIMEOUT_S = 3600
MAX_BATCH = 4
POLL_S = 1.0


def contract(target: str, change: str, acceptance: str) -> str:
    """The child's goal text — the contract in the shape the loop reads."""
    return (f"# Target\n{target.strip()}\n\n# Change\n{change.strip()}\n\n# Acceptance\n{acceptance.strip()}\n\n"
            "You are a subagent with a blank context: everything you need is in this contract and the "
            "workspace. Work until the acceptance criteria hold, then reply DONE: with a summary of what "
            "you did, what you found (facts with file paths / URLs), and what is left. Do not ask questions; "
            "if something is impossible, reply BLOCKED: and why.")


async def _run_child(goal: str, timeout_s: int) -> dict:
    """Create a discrete task and wait for it to end; returns facts."""
    from seymour.agent.discrete import discrete
    from seymour.db import AgentTask, SessionLocal
    started = time.monotonic()
    created = await discrete.create(goal, session_id="")
    task_id = created["id"]
    status = created["status"]
    while time.monotonic() - started < timeout_s:
        await asyncio.sleep(POLL_S)
        with SessionLocal() as db:
            row = db.get(AgentTask, task_id)
            status = row.status if row else "unknown"
            result = (row.result or "") if row else ""
        if status in ("done", "blocked", "paused", "cancelled"):
            break
    else:
        # Out of time: stop it; whatever it journaled is still the artifact.
        try:
            await discrete.cancel(task_id)
        except (KeyError, ValueError):
            pass
        status, result = "timed out", ""
    return {"task_id": task_id, "status": status, "result": result, "seconds": round(time.monotonic() - started, 1)}


def _write_artifact(task_id: str) -> tuple[str, int]:
    """The child's whole journal as a Markdown artifact; (path, steps)."""
    from seymour.db import AgentTask, SessionLocal
    with SessionLocal() as db:
        row = db.get(AgentTask, task_id)
        if row is None:
            return "", 0
        lines = [f"# Subagent task {task_id}", "", "## Contract", "", row.goal, "", f"## Status: {row.status}", ""]
        if row.notes:
            lines += ["## Notes", "", row.notes, ""]
        lines += ["## Journal", ""]
        for step in row.steps:
            lines += [f"### [{step.kind}]", "", step.content, ""]
        if row.result:
            lines += ["## Result", "", row.result, ""]
        steps = len(row.steps)
    target = paths.artifacts_dir() / f"task-{task_id[:8]}.md"
    target.write_text("\n".join(lines), encoding="utf-8")
    return paths.display(target), steps


def _summary(facts: dict) -> str:
    path, steps = _write_artifact(facts["task_id"])
    body = facts["result"].strip() or "(no report — see the artifact for what happened)"
    if len(body) > MAX_SUMMARY_CHARS:
        body = body[:MAX_SUMMARY_CHARS] + f"\n[… summary cut at {MAX_SUMMARY_CHARS} chars; the artifact has the rest]"
    return (f"[subagent {facts['task_id'][:8]} · {facts['status']} · {steps} steps · {facts['seconds']}s]\n"
            f"{body}\n[full journal: {path} — read_file it for the details]")


def _guard() -> str | None:
    if context.depth.get() >= 1:
        return ("Error: a subagent may not start subagents. Do the work yourself, or reply DONE: with "
                "what you have and let the parent decide.")
    return None


async def task(target: str, change: str, acceptance: str, timeout_s: str | int = "") -> str:
    """Tool entry: one child run, awaited."""
    refused = _guard()
    if refused:
        return refused
    if not (target or "").strip() or not (change or "").strip():
        return "Error: task needs target and change (and ideally acceptance)."
    try:
        timeout = max(30, min(int(str(timeout_s).strip() or DEFAULT_TIMEOUT_S), MAX_TIMEOUT_S))
    except ValueError:
        timeout = DEFAULT_TIMEOUT_S
    goal = contract(target, change, acceptance or "The change is made and verified (tests run, file checked).")
    facts = await _run_child(goal, timeout)
    return _summary(facts)


async def tasks(batch: str, timeout_s: str | int = "") -> str:
    """Tool entry: several children at once, all summaries returned."""
    refused = _guard()
    if refused:
        return refused
    try:
        items = json.loads(batch) if isinstance(batch, str) else batch
    except json.JSONDecodeError as error:
        return f"Error: batch must be a JSON list of {{target, change, acceptance}}: {error}"
    if not isinstance(items, list) or not items:
        return "Error: batch must be a non-empty JSON list of {target, change, acceptance}."
    if len(items) > MAX_BATCH:
        return f"Error: at most {MAX_BATCH} tasks at once (got {len(items)})."
    try:
        timeout = max(30, min(int(str(timeout_s).strip() or DEFAULT_TIMEOUT_S), MAX_TIMEOUT_S))
    except ValueError:
        timeout = DEFAULT_TIMEOUT_S
    goals = []
    for item in items:
        if not isinstance(item, dict) or not str(item.get("target", "")).strip():
            return "Error: every task needs at least a target and a change."
        goals.append(contract(str(item.get("target", "")), str(item.get("change", "")),
                              str(item.get("acceptance", "")) or "The change is made and verified."))
    results = await asyncio.gather(*(_run_child(goal, timeout) for goal in goals))
    return "\n\n".join(f"## Task {i + 1}: {str(items[i].get('target', ''))[:80]}\n{_summary(facts)}"
                       for i, facts in enumerate(results))


def _d_task(args: dict) -> str:
    return f"start a subagent on: {str(args.get('target') or args.get('batch') or '')[:80]}"


from seymour.tools import Tool                      # noqa: E402  (registry type)

TOOLS = [
    Tool(
        name="task",
        description=("Delegate a self-contained piece of work to a SUBAGENT with a blank context: it "
                     "gets your contract (target / change / acceptance), the same tools and workspace, "
                     "works until done, and you get a short summary plus the path of its full journal — "
                     "your own context stays small. Use it for research that would flood your context, "
                     "or a change in files you do not need to read yourself. Give a precise acceptance "
                     "criterion (tests green, a list of facts with sources)."),
        args={"target": "what to work on (files, a question, a page)",
              "change": "what to do or find out",
              "acceptance": "how the subagent knows it is done",
              "timeout_s": f"seconds to allow (default {DEFAULT_TIMEOUT_S})"},
        optional=frozenset({"acceptance", "timeout_s"}),
        tier="exec", func=task, describe=_d_task,
    ),
    Tool(
        name="tasks",
        description=(f"Run up to {MAX_BATCH} independent subagent tasks AT ONCE (each a "
                     "{target, change, acceptance} object) and get all their summaries together. "
                     "For parallel research or changes in unrelated files."),
        args={"batch": 'JSON list of {"target": "...", "change": "...", "acceptance": "..."}',
              "timeout_s": f"seconds to allow each (default {DEFAULT_TIMEOUT_S})"},
        optional=frozenset({"timeout_s"}),
        tier="exec", func=tasks, describe=_d_task,
    ),
]
