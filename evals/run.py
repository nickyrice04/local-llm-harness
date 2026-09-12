"""The eval runner: drive the REAL app over HTTP, score with REAL checks.

See EVALS.md for the rules. The short version: the task list below is
frozen (EVAL_SET_VERSION guards it), every criterion is programmatic,
and failures are results — a red row is information, not embarrassment.

Usage:  .venv/bin/python evals/run.py --label baseline
Needs the app on 127.0.0.1:8765 with a model loaded.
"""

import argparse
import asyncio
import json
import re
import time
from datetime import date
from pathlib import Path

import httpx

# Bump when ANY task or criterion changes — comparisons are only valid
# within one version (EVALS.md's first rule).
# v2: markdown-honesty's phrase list gained "can't see"/"cannot see" —
#     the v1 run proved the model honest ("I can't see any file
#     attached…") and the CHECK wrong. No task/prompt changes.
# v4: text checks normalise typographic quotes/dashes — "doesn’t exist"
#     must satisfy a check written "doesn't exist". Another checker bug
#     the runs exposed; no task or prompt changed.
# v3: cancel-mid-run's "slots freed" now watches the SCHEDULER's ledger
#     for the research label (settling up to 20 s) instead of counting
#     raw llama slots — background follow-ups (title, memory) may
#     legitimately be decoding, and v2 red-flagged one by accident.
# v5: the harness rebuild's tool set (2026-08-20) — five ADDED tasks in a
#     new "coding" category (edit-by-lines, run tests, grep, focused
#     fetch, honest exit codes) plus the eval client auto-answering the
#     write/exec approval gate. Existing tasks are untouched, so their
#     rows compare across v4/v5; only the totals differ.
EVAL_SET_VERSION = 5

BASE = "http://127.0.0.1:8765"
ENGINE = "http://127.0.0.1:8080"
WORKSPACE = Path.home() / ".seymour" / "workspace"
RESULTS_DIR = Path(__file__).parent / "results"

# Per-kind ceilings (seconds). Generous: a 35B on battery is not fast,
# and a timeout is recorded as a failure, not a crash.
TIMEOUTS = {"chat": 180, "agent": 300, "research": 600}


# --------------------------------------------------------------------- client
class App:
    """A thin async client for Seymour's HTTP surface."""

    def __init__(self) -> None:
        self.http = httpx.AsyncClient(base_url=BASE, timeout=30.0)

    async def close(self) -> None:
        await self.http.aclose()

    async def chat(self, message: str, session_id: str | None = None,
                   attachments: list[str] | None = None) -> dict:
        """One chat-mode message, SSE consumed to the end. Returns
        {reply, stats, session_id, error, notice}."""
        out = {"reply": "", "stats": {}, "session_id": session_id,
               "error": "", "notice": ""}
        body = {"session_id": session_id, "message": message,
                "mode": "chat", "attachments": attachments or []}
        async with self.http.stream("POST", "/api/chat", json=body,
                                    timeout=TIMEOUTS["chat"]) as response:
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload == "[DONE]":
                    break
                frame = json.loads(payload)
                if "session_id" in frame:
                    out["session_id"] = frame["session_id"]
                if "approval" in frame:
                    # The write/exec gate asks the person once per run;
                    # the eval IS the person and says yes (recorded, so
                    # a task can assert that it was asked).
                    out["approvals"] = out.get("approvals", 0) + 1
                    await self.http.post(
                        f"/api/runs/{frame['approval']['run_id']}/approve",
                        json={"allow": True})
                if "notice" in frame:
                    out["notice"] += frame["notice"]
                if "replace" in frame:
                    out["reply"] = frame["replace"]
                if "delta" in frame:
                    out["reply"] += frame["delta"]
                if "stats" in frame:
                    out["stats"] = frame["stats"]
                if "error" in frame:
                    out["error"] += frame["error"]
        return out

    async def start_job(self, message: str, mode: str) -> dict:
        """Agent/research mode: returns {session_id, job:{kind,id}, note}."""
        response = await self.http.post("/api/chat", json={
            "session_id": None, "message": message, "mode": mode,
            "attachments": []})
        response.raise_for_status()
        return response.json()

    async def wait_task(self, task_id: str, timeout: float) -> dict:
        """Poll the discrete-task list until terminal (or timeout)."""
        deadline = time.monotonic() + timeout
        last = {}
        while time.monotonic() < deadline:
            data = (await self.http.get("/api/tasks")).json()
            rows = [t for t in data["tasks"] if t["id"] == task_id]
            if rows:
                last = rows[0]
                if last["status"] in ("done", "failed", "cancelled",
                                      "paused", "blocked"):
                    return last
            await asyncio.sleep(3)
        last.setdefault("status", "timeout")
        last["status"] = last.get("status") or "timeout"
        return last

    async def wait_research(self, job_id: str, timeout: float) -> dict:
        """Poll the research list until terminal (or timeout)."""
        deadline = time.monotonic() + timeout
        last = {}
        while time.monotonic() < deadline:
            rows = [r for r in (await self.http.get("/api/research")).json()
                    if r["id"] == job_id]
            if rows:
                last = rows[0]
                if last["status"] != "running":
                    return last
            await asyncio.sleep(4)
        last.setdefault("status", "timeout")
        return last

    async def session_messages(self, session_id: str) -> list[dict]:
        response = await self.http.get(f"/api/sessions/{session_id}")
        response.raise_for_status()
        return response.json()["messages"]

    async def upload_text(self, name: str, text: str) -> str:
        """Upload a text document; returns the attachment id."""
        response = await self.http.post(
            "/api/upload", files={"file": (name, text.encode(), "text/plain")})
        response.raise_for_status()
        return response.json()["id"]

    async def engine_busy_slots(self) -> int:
        """How many llama-server slots are decoding right now (-1 if the
        engine isn't reachable — recorded, never fatal)."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as raw:
                slots = (await raw.get(f"{ENGINE}/slots")).json()
            return sum(1 for s in slots if s.get("is_processing"))
        except Exception:
            return -1


# --------------------------------------------------------------------- checks
# Every check is (name, fn(ctx) -> (ok, detail)). ctx is the dict the task
# runner filled: reply/stats/task/job/messages/expected/... — whatever the
# task's kind produces.
def _norm(text: str) -> str:
    """Lowercase, and flatten the typographic quotes models actually
    emit. Without this, a model writing "doesn’t exist" fails a check
    looking for "doesn't exist" — measured, and a pure checker bug: the
    ANSWER was right. Criteria must test meaning, not punctuation."""
    return (str(text).lower()
            .replace("’", "'").replace("‘", "'")
            .replace("“", '"').replace("”", '"')
            .replace("—", "-").replace("–", "-"))


def contains_any(*needles, field="reply"):
    def check(ctx):
        text = _norm(ctx.get(field, ""))
        ok = any(_norm(n) in text for n in needles)
        return ok, f"looked for {needles} in {field} ({len(text)} chars)"
    return (f"contains_any{needles}", check)


def not_contains(*needles, field="reply"):
    def check(ctx):
        text = _norm(ctx.get(field, ""))
        hits = [n for n in needles if _norm(n) in text]
        return not hits, f"forbidden {hits or needles} in {field}"
    return (f"not_contains{needles}", check)


def reply_max_chars(limit):
    def check(ctx):
        n = len(ctx.get("reply", ""))
        return n <= limit, f"reply is {n} chars (limit {limit})"
    return (f"reply<= {limit}", check)


def tool_rounds(minimum=0, maximum=99):
    def check(ctx):
        n = ctx.get("stats", {}).get("tool_rounds", 0)
        return minimum <= n <= maximum, f"tool_rounds={n} (want {minimum}..{maximum})"
    return (f"tool_rounds {minimum}..{maximum}", check)


def workspace_file(name, *needles, min_lines=0):
    def check(ctx):
        path = WORKSPACE / name
        if not path.exists():
            return False, f"{name} does not exist"
        text = path.read_text(encoding="utf-8", errors="replace")
        missing = [n for n in needles if n.lower() not in text.lower()]
        lines = len([l for l in text.splitlines() if l.strip()])
        ok = not missing and lines >= min_lines
        return ok, f"{name}: {lines} lines, missing={missing}"
    return (f"file {name}", check)


def status_is(*allowed, field="status"):
    def check(ctx):
        got = (ctx.get("task") or ctx.get("job") or {}).get(field, "?")
        return got in allowed, f"status={got} (want {allowed})"
    return (f"status in {allowed}", check)


def sources_min(minimum):
    def check(ctx):
        n = (ctx.get("job") or {}).get("sources", 0)
        return n >= minimum, f"sources={n} (want >= {minimum})"
    return (f"sources >= {minimum}", check)


def last_message_contains(*needles):
    def check(ctx):
        msgs = ctx.get("messages") or []
        text = msgs[-1]["content"].lower() if msgs else ""
        ok = any(n.lower() in text for n in needles)
        return ok, f"last message {len(text)} chars; looked for {needles}"
    return (f"last_msg has {needles}", check)


def regex_count(pattern, minimum, field="reply"):
    def check(ctx):
        n = len(re.findall(pattern, str(ctx.get(field, "")), re.MULTILINE))
        return n >= minimum, f"{n} matches of {pattern!r} (want >= {minimum})"
    return (f"regex x{minimum}", check)


def custom(name, fn):
    return (name, fn)


# ---------------------------------------------------------------- the set
# THE FROZEN LIST. Every entry: id, category, kind, prompt, checks, and
# optionally setup(app) -> dict merged into ctx. Order matters only for
# the last two (they disturb global state, so they run last).
def long_doc() -> str:
    """A deterministic ~16k-char document with facts planted at the
    start, middle, and end, plus a needle at ~80%."""
    filler = ("Quarterly operations continued according to plan, with "
              "routine maintenance and unremarkable metrics recorded "
              "throughout the period under review. ")
    parts = ["PROJECT BRIEFING.\n",
             "The first initiative is codenamed BLUEMARROW.\n",
             filler * 60,
             "The second initiative is codenamed TIDEWALKER.\n",
             filler * 60]
    tail = [filler * 12,
            "Note for operators: the deploy password is korma-7.\n",
            filler * 12,
            "The third initiative is codenamed OATCREST.\n"]
    return "".join(parts + tail)


async def setup_upload_doc(app: App) -> dict:
    return {"attachment": await app.upload_text("eval-briefing.txt", long_doc())}


async def setup_seed_file(app: App) -> dict:
    (WORKSPACE / "eval-notes.md").write_text("draft one\n", encoding="utf-8")
    return {}


async def setup_count_md(app: App) -> dict:
    return {"expected_count": len(list(WORKSPACE.glob("*.md")))}


BUGGY_CALC = ("def add(a, b):\n    return a - b\n\n\n"
              "if __name__ == '__main__':\n"
              "    assert add(2, 2) == 4, 'add is broken'\n"
              "    print('all good')\n")


async def setup_buggy_calc(app: App) -> dict:
    """A tiny program with one wrong operator and a self-test that fails."""
    (WORKSPACE / "eval-calc.py").write_text(BUGGY_CALC, encoding="utf-8")
    return {}


async def setup_grep_targets(app: App) -> dict:
    """Three files; exactly one mentions the codename."""
    (WORKSPACE / "eval-g1.txt").write_text("routine notes, nothing special\n")
    (WORKSPACE / "eval-g2.txt").write_text("the launch is codenamed TIDEWALKER\n")
    (WORKSPACE / "eval-g3.txt").write_text("more routine notes\n")
    return {}


TASKS: list[dict] = [
    # ---- tool-use --------------------------------------------------------
    dict(id="web-fact", category="tool-use", kind="chat",
         prompt="who won the 2026 fifa world cup?",
         checks=[contains_any("spain"), tool_rounds(minimum=1)]),
    dict(id="fetch-url", category="tool-use", kind="chat",
         prompt="fetch https://example.com and tell me in one sentence what that page is for",
         checks=[contains_any("illustrative", "example", "documentation"),
                 tool_rounds(minimum=1)]),
    dict(id="no-tool-discipline", category="tool-use", kind="chat",
         prompt="explain what a mutex is in two sentences",
         checks=[contains_any("lock", "exclusive", "thread"),
                 tool_rounds(maximum=0)]),
    dict(id="news-concise", category="tool-use", kind="chat",
         prompt="give me the most recent news article you can find",
         checks=[tool_rounds(minimum=1), reply_max_chars(2500),
                 contains_any("http")]),
    dict(id="gfm-table", category="tool-use", kind="chat",
         prompt="make a markdown table of three planets and their diameters in km",
         checks=[regex_count(r"^\|.*\|.*\|", 3)]),
    # ---- multi-step (agent mode) ----------------------------------------
    dict(id="agent-write", category="multi-step", kind="agent",
         prompt="write a haiku about tea into eval-tea-haiku.md in the workspace, then finish",
         checks=[status_is("done"), workspace_file("eval-tea-haiku.md", min_lines=3)]),
    dict(id="agent-two-files", category="multi-step", kind="agent",
         prompt="create eval-notes-a.md containing the word alpha and eval-notes-b.md containing the word beta in the workspace, then finish",
         checks=[status_is("done"),
                 workspace_file("eval-notes-a.md", "alpha"),
                 workspace_file("eval-notes-b.md", "beta")]),
    dict(id="agent-append", category="multi-step", kind="agent",
         prompt="append the single line reviewed to the end of eval-notes.md in the workspace, keeping its current content, then finish",
         setup=setup_seed_file,
         checks=[status_is("done"),
                 workspace_file("eval-notes.md", "draft one", "reviewed")]),
    dict(id="agent-count", category="multi-step", kind="agent",
         prompt="count how many .md files are in the workspace and write just that number into eval-count.txt, then finish",
         setup=setup_count_md,
         checks=[status_is("done"),
                 custom("count matches", lambda ctx: (
                     (WORKSPACE / "eval-count.txt").exists()
                     and str(ctx["expected_count"]) in
                     (WORKSPACE / "eval-count.txt").read_text(),
                     f"expected {ctx.get('expected_count')}"))]),
    dict(id="agent-list", category="multi-step", kind="agent",
         prompt="list the names of the files currently in the workspace and finish with that list as your result",
         checks=[status_is("done"),
                 custom("names real files", lambda ctx: (
                     sum(1 for p in WORKSPACE.iterdir()
                         if p.name in (ctx.get("task") or {}).get("result", "")) >= 2,
                     "want >=2 real filenames in the result"))]),
    # ---- coding (the harness rebuild's tool set, v5) --------------------
    dict(id="code-fix-chat", category="coding", kind="chat",
         prompt="eval-calc.py in the workspace has a failing self-test. run it, "
                "fix the bug, run it again until it passes, and tell me what "
                "was wrong",
         setup=setup_buggy_calc,
         checks=[workspace_file("eval-calc.py", "a + b"),
                 tool_rounds(minimum=3),
                 contains_any("all good", "pass", "exit code 0", "fixed", "minus", "subtract", "- b")]),
    dict(id="code-fix-agent", category="coding", kind="agent",
         prompt="eval-calc.py in the workspace has a failing self-test. Run it "
                "with run_command, fix the bug with edit_lines, run it again "
                "until it passes, then finish with what was wrong",
         setup=setup_buggy_calc,
         checks=[status_is("done"), workspace_file("eval-calc.py", "a + b")]),
    dict(id="grep-find", category="coding", kind="chat",
         prompt="which file in the workspace mentions TIDEWALKER? answer with just the filename",
         setup=setup_grep_targets,
         checks=[contains_any("eval-g2.txt"), not_contains("eval-g1.txt", "eval-g3.txt"),
                 tool_rounds(minimum=1)]),
    dict(id="fetch-focus", category="coding", kind="chat",
         prompt="fetch https://en.wikipedia.org/wiki/Mutual_exclusion and tell me "
                "in one sentence who first identified the mutual exclusion problem",
         checks=[contains_any("dijkstra"), tool_rounds(minimum=1)]),
    dict(id="exit-code-honesty", category="coding", kind="chat",
         prompt="run this exact command in the workspace and tell me the exit "
                "code it returned: python -c \"import sys; sys.exit(7)\"",
         checks=[contains_any("7"), tool_rounds(minimum=1)]),
    # ---- long-context ----------------------------------------------------
    dict(id="doc-codenames", category="long-context", kind="chat",
         prompt="what are the three initiative codenames in the attached briefing? answer with just the three names",
         setup=setup_upload_doc, attach=True,
         checks=[contains_any("bluemarrow"), contains_any("tidewalker"),
                 contains_any("oatcrest")]),
    dict(id="doc-needle", category="long-context", kind="chat",
         prompt="what is the deploy password mentioned in the attached briefing?",
         setup=setup_upload_doc, attach=True,
         checks=[contains_any("korma-7")]),
    dict(id="convo-memory", category="long-context", kind="multi-chat",
         prompts=["for later: my cat is named Waffles and she loves cardboard boxes",
                  "what is 17 + 26? just the number please",
                  "name one use for a paperclip, briefly",
                  "what is my cat's name?"],
         checks=[contains_any("waffles")]),
    # ---- research --------------------------------------------------------
    dict(id="research-fact", category="research", kind="research",
         prompt="was covid made in a lab?",
         checks=[status_is("done"), sources_min(2),
                 custom("delivered to chat",
                        lambda ctx: (any("research" in m["content"].lower()
                                         and m["role"] == "assistant"
                                         for m in ctx.get("messages", [])[-2:]),
                                     "outcome message in conversation"))]),
    dict(id="research-compare", category="research", kind="research",
         prompt="compare sqlite and duckdb for analytics workloads",
         checks=[status_is("done"), sources_min(2),
                 custom("mentions both", lambda ctx: (
                     "sqlite" in str(ctx.get("messages", []))[-6000:].lower()
                     and "duckdb" in str(ctx.get("messages", []))[-6000:].lower(),
                     "both names in the delivered report"))]),
    dict(id="research-recent", category="research", kind="research",
         prompt="what did apple announce at its most recent event?",
         checks=[status_is("done"), sources_min(2)]),
    # ---- recovery --------------------------------------------------------
    dict(id="absent-tool", category="recovery", kind="chat",
         prompt="use your calculator tool to compute 17 * 23 and give me the result",
         checks=[contains_any("391")]),
    dict(id="dead-url", category="recovery", kind="chat",
         prompt="fetch https://this-site-definitely-does-not-exist-xk9q2.com and summarize it",
         checks=[contains_any("fail", "couldn't", "could not", "unable",
                              "error", "doesn't exist", "does not exist",
                              "unreachable", "not accessible")]),
    dict(id="junk-research", category="recovery", kind="research",
         prompt="hello",
         # The 4.4 rule: a contentless message must never produce a
         # research run that reports "done". KNOWN-RED at baseline —
         # this is a target for the executor convergence, kept honest.
         checks=[status_is("failed", "cancelled", "declined")]),
    dict(id="markdown-honesty", category="recovery", kind="chat",
         prompt="what does the attached file say? (note: I attached nothing)",
         checks=[not_contains("<<<UNTRUSTED"), contains_any(
             "no file", "nothing attached", "don't see", "do not see",
             "can't see", "cannot see", "no attach", "didn't attach",
             "did not attach", "no document")]),
    # The last two disturb global state (contention, cancellation): LAST.
    dict(id="contention", category="recovery", kind="contention",
         checks=[]),          # custom-run; checks built inline
    dict(id="cancel-mid-run", category="recovery", kind="cancel",
         checks=[]),
]


# --------------------------------------------------------------------- runner
async def run_task(app: App, task: dict) -> dict:
    """Execute one task; never raises — an exception is a failed row."""
    started = time.monotonic()
    ctx: dict = {}
    record = {"id": task["id"], "category": task["category"],
              "passed": False, "checks": [], "seconds": 0.0, "error": ""}
    try:
        if "setup" in task:
            ctx.update(await task["setup"](app))

        if task["kind"] == "chat":
            attachments = [ctx["attachment"]] if task.get("attach") else None
            result = await asyncio.wait_for(
                app.chat(task["prompt"], attachments=attachments),
                TIMEOUTS["chat"] + 30)
            ctx.update(result)
        elif task["kind"] == "multi-chat":
            session = None
            for prompt in task["prompts"]:
                result = await asyncio.wait_for(
                    app.chat(prompt, session_id=session), TIMEOUTS["chat"] + 30)
                session = result["session_id"]
            ctx.update(result)
        elif task["kind"] == "agent":
            started_job = await app.start_job(task["prompt"], "agent")
            ctx["task"] = await app.wait_task(started_job["job"]["id"],
                                             TIMEOUTS["agent"])
        elif task["kind"] == "research":
            started_job = await app.start_job(task["prompt"], "research")
            ctx["job"] = await app.wait_research(started_job["job"]["id"],
                                                 TIMEOUTS["research"])
            ctx["messages"] = await app.session_messages(
                started_job["session_id"])
        elif task["kind"] == "contention":
            # Two live chats + one agent task, all at once: everything
            # must complete (the scheduler's whole reason to exist).
            job = await app.start_job(
                "write one sentence about rain into eval-rain.md in the "
                "workspace, then finish", "agent")
            chat_a, chat_b = await asyncio.gather(
                app.chat("say the word ready and stop"),
                app.chat("what is 5 + 5? just the number"))
            done = await app.wait_task(job["job"]["id"], TIMEOUTS["agent"])
            task["checks"] = [
                custom("chat A replied", lambda c: (bool(chat_a["reply"].strip()),
                                                    chat_a["reply"][:60])),
                custom("chat B replied", lambda c: ("10" in chat_b["reply"],
                                                    chat_b["reply"][:60])),
                custom("task finished", lambda c: (done["status"] == "done",
                                                   f"status={done['status']}")),
            ]
        elif task["kind"] == "cancel":
            # Start research, cancel mid-run: it must end 'cancelled',
            # deliver an outcome message, and FREE the engine's slots.
            job = await app.start_job(
                "history of the gameboy advance launch", "research")
            await asyncio.sleep(8)
            await app.http.post(f"/api/research/{job['job']['id']}/cancel")
            final = await app.wait_research(job["job"]["id"], 60)
            # The precise question is "did THIS run's stream leave the
            # ledger?" — raw llama slot counts also see unrelated
            # background work (title/memory follow-ups). Settle ≤ 20 s.
            freed = False
            for _ in range(10):
                snap = (await app.http.get("/api/status")).json()["scheduler"]
                labels = [t["label"] for t in snap.get("active", [])]
                if not any(l.startswith("research:") for l in labels):
                    freed = True
                    break
                await asyncio.sleep(2)
            busy = await app.engine_busy_slots()   # informational only
            messages = await app.session_messages(job["session_id"])
            task["checks"] = [
                custom("run cancelled", lambda c: (final["status"] == "cancelled",
                                                   f"status={final['status']}")),
                custom("outcome in chat", lambda c: (
                    any("research" in m["content"].lower()
                        for m in messages[-2:]), f"{len(messages)} messages")),
                custom("stream left the ledger", lambda c: (
                    freed, f"freed={freed}, raw busy slots={busy}")),
            ]

        for name, fn in task["checks"]:
            try:
                ok, detail = fn(ctx)
            except Exception as error:          # a broken check is a failure
                ok, detail = False, f"check crashed: {error}"
            record["checks"].append({"name": name, "ok": bool(ok),
                                     "detail": str(detail)[:200]})
        record["passed"] = bool(record["checks"]) and all(
            c["ok"] for c in record["checks"])
        if "reply" in ctx:
            record["reply_excerpt"] = ctx["reply"][:300]
    except Exception as error:
        record["error"] = f"{type(error).__name__}: {error}"
    record["seconds"] = round(time.monotonic() - started, 1)
    return record


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="run")
    parser.add_argument("--only", default="",
                        help="comma-separated task ids (debugging aid)")
    args = parser.parse_args()

    app = App()
    status = (await app.http.get("/api/status")).json()
    if not status.get("capabilities"):
        raise SystemExit("no model loaded — load one first")

    # Clean this run's artifacts (eval- prefix only; never user files).
    for stale in WORKSPACE.glob("eval-*"):
        stale.unlink()

    conditions = {
        "model": status["capabilities"].get("model_id"),
        "mode": status["scheduler"].get("mode"),
        "on_battery": status["battery"]["on_battery"],
        "battery_percent": status["battery"]["percent"],
    }
    tasks = [t for t in TASKS
             if not args.only or t["id"] in args.only.split(",")]
    print(f"eval set v{EVAL_SET_VERSION} — {len(tasks)} tasks — {conditions}")

    records = []
    for task in tasks:
        record = await run_task(app, task)
        records.append(record)
        mark = "PASS" if record["passed"] else "FAIL"
        why = "" if record["passed"] else " | " + "; ".join(
            f"{c['name']}: {c['detail']}" for c in record["checks"]
            if not c["ok"])[:160] + record["error"][:120]
        print(f"[{mark}] {record['id']:<18} {record['seconds']:>6.1f}s{why}")

    by_cat: dict = {}
    for record in records:
        cat = by_cat.setdefault(record["category"], [0, 0])
        cat[1] += 1
        cat[0] += int(record["passed"])
    total = sum(r["passed"] for r in records)
    print("\n---- summary ----")
    for cat, (p, n) in by_cat.items():
        print(f"{cat:<14} {p}/{n}")
    print(f"{'TOTAL':<14} {total}/{len(records)}")

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"{args.label}-{date.today().isoformat()}.json"
    out.write_text(json.dumps({
        "version": EVAL_SET_VERSION, "label": args.label,
        "date": date.today().isoformat(), "conditions": conditions,
        "tasks": records,
        "summary": {"by_category": {k: f"{v[0]}/{v[1]}" for k, v in by_cat.items()},
                    "total": f"{total}/{len(records)}"},
    }, indent=2), encoding="utf-8")
    print(f"\nwritten: {out}")
    await app.close()


if __name__ == "__main__":
    asyncio.run(main())
