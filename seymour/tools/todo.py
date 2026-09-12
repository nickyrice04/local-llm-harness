"""todo_write — the plan, kept by the harness, outside the context window.

The single biggest reliability gain for a 27–35B on multi-step work
(dsh's `packages/todo`, oh-my-pi's `todo`): the model writes its plan
once, updates it as items start and finish, and the harness keeps it —
so it survives compaction (the summary block re-states it verbatim),
shows in the chat as a panel ("what is it doing?" answered at a glance)
and is echoed back on every write so the model's own view stays exact.

Two input shapes, because a small model reaches for both:

    {"todos": [{"content": "read the failing test", "status": "done"},
               {"content": "fix the off-by-one", "status": "in_progress"}]}

    {"todos": "[x] read the failing test\\n[>] fix the off-by-one\\n[ ] run pytest"}

Statuses: pending | in_progress | done. Anything else is coerced to
pending and said so.
"""

import json
import re

from seymour.tools import context

# Per-run plans: run id → list of {"content", "status"}. Process-local;
# bounded by the number of runs a session starts (entries are dropped
# when a run ends — the executor calls `forget`).
_plans: dict[str, list[dict]] = {}
MAX_ITEMS = 40
STATUSES = ("pending", "in_progress", "done")
_MARKS = {"[ ]": "pending", "[>]": "in_progress", "[x]": "done", "[X]": "done", "[-]": "done"}
_LINE = re.compile(r"^\s*(?:[-*]\s*)?(\[[ >xX-]\])\s*(.+?)\s*$")


def parse(raw) -> tuple[list[dict], list[str]]:
    """Turn either input shape into a clean list; returns (items, notes)."""
    notes: list[str] = []
    items: list[dict] = []
    data = raw
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("["):
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = None
        else:
            data = None
        if data is None:
            # The checkbox form, one item per line.
            for line in text.splitlines():
                m = _LINE.match(line)
                if m:
                    items.append({"content": m.group(2), "status": _MARKS.get(m.group(1), "pending")})
                elif line.strip():
                    items.append({"content": line.strip(), "status": "pending"})
            return items[:MAX_ITEMS], notes
    if isinstance(data, dict) and "todos" in data:
        data = data["todos"]
    if not isinstance(data, list):
        return [], ["todos must be a JSON list of {content, status} or checkbox lines ([ ] / [>] / [x])"]
    for entry in data[:MAX_ITEMS]:
        if isinstance(entry, str):
            items.append({"content": entry.strip(), "status": "pending"})
            continue
        if not isinstance(entry, dict):
            continue
        content = str(entry.get("content") or entry.get("task") or entry.get("text") or "").strip()
        if not content:
            continue
        status = str(entry.get("status") or "pending").strip().lower().replace("-", "_").replace(" ", "_")
        if status in ("completed", "complete", "finished"):
            status = "done"
        if status in ("active", "doing", "started", "current"):
            status = "in_progress"
        if status not in STATUSES:
            notes.append(f"status {status!r} on {content[:40]!r} is not one of pending/in_progress/done — set to pending")
            status = "pending"
        items.append({"content": content, "status": status})
    return items, notes


def render(items: list[dict]) -> str:
    """The plan as text — the tool result, and the compaction block."""
    if not items:
        return "(no plan)"
    marks = {"pending": "[ ]", "in_progress": "[>]", "done": "[x]"}
    done = sum(1 for i in items if i["status"] == "done")
    lines = [f"{marks[i['status']]} {i['content']}" for i in items]
    return "\n".join(lines) + f"\n({done}/{len(items)} done)"


def current(run: str | None = None) -> list[dict]:
    """The plan of a run (default: the calling run)."""
    return list(_plans.get(run or context.run_id.get(), []))


def forget(run: str) -> None:
    _plans.pop(run, None)


async def todo_write(todos: str = "") -> str:
    """Tool entry: replace the run's plan with this list."""
    items, notes = parse(todos)
    if not items:
        return ("Error: todo_write needs the whole plan: a JSON list of "
                '{"content": "...", "status": "pending|in_progress|done"} (or checkbox lines). '
                + " ".join(notes))
    in_progress = sum(1 for i in items if i["status"] == "in_progress")
    if in_progress > 1:
        notes.append(f"{in_progress} items are in_progress — one at a time is the discipline")
    run = context.run_id.get()
    _plans[run] = items
    text = "Plan updated:\n" + render(items)
    if notes:
        text += "\n[note: " + "; ".join(notes) + "]"
    return text


from seymour.tools import Tool                      # noqa: E402  (registry type)

TOOLS = [
    Tool(
        name="todo_write",
        description=("Write or update your PLAN for a multi-step task — the whole list each time, "
                     "each item with a status (pending | in_progress | done). Do this first on any task "
                     "with 3+ steps, mark an item in_progress when you start it and done when its "
                     "result is in. The harness keeps the plan for you (it survives context "
                     "compaction) and shows it to your person."),
        args={"todos": 'JSON list of {"content": "...", "status": "pending|in_progress|done"}, '
                       "or lines starting with [ ] / [>] / [x]"},
        tier="read", func=todo_write,
    ),
]
