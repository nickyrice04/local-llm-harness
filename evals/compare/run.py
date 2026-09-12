"""Harness comparison: the same tasks through Seymour and deepseek-harness
on the same llama-server, scored the same way.

    .venv/bin/python evals/compare/run.py --label first            # both
    .venv/bin/python evals/compare/run.py --harness seymour --only html-desktop-os

Seymour side: each task becomes a discrete agent task (POST /api/tasks —
Tier 2, thinking on by default, no approval gate, the same loop the
primary agent runs). Its files are placed in Seymour's real workspace
(the tools are confined there) and removed afterwards.

dsh side: the published Python SDK runs `examples/jsonrpc-agent/minimal.py`
(persistent bash + str_replace_editor, danger-full-access) in a fresh
directory, pointed at llama-server's OpenAI-compatible endpoint through
DEEPSEEK_BASE_URL. dsh's reasoning_effort/thinking fields are ignored by
llama-server, so BOTH harnesses run with the model's thinking ON — that
is the parity condition; the Inference settings' thinking switch is set
to "auto" for the duration.

Checks are programmatic (evals/compare/tasks.py). Outputs are copied to
evals/results/compare-<label>/<harness>/<task>/ so a person can open the
two solar systems side by side; the report links them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tasks import TASKS, Task  # noqa: E402

BASE = "http://127.0.0.1:8765"
ENGINE = "http://127.0.0.1:8080"      # llama-server (the eval's default engine)
# Where dsh's SDK sends its OpenAI-compatible calls. Both of Seymour's
# engines speak that API: llama-server on 8080, the MLX server on 8082
# (--dsh-base-url picks; the app's /api/status names the engine).
DSH_BASE_URL = "http://127.0.0.1:8080/v1"
WORKSPACE = Path.home() / ".seymour" / "workspace"
REPO = Path(__file__).resolve().parents[2]
RESULTS = REPO / "evals" / "results"
# The dsh SDK lives in its own venv (it pins pydantic); the checked-out
# deepseek-harness repo supplies the minimal composition.
DSH_VENV = Path(os.environ.get("DSH_VENV", "/private/tmp/claude-501/-Users-nickyrice-Documents-seymour-guide/8f573ded-3685-478f-b66f-b72a4ea2276a/scratchpad/dsh/dsh-venv"))
DSH_REPO = Path(os.environ.get("DSH_REPO", REPO.parent / "deepseek-harness-src"))


# ------------------------------------------------------------- scoring
def score(task: Task, workspace: Path) -> tuple[float, list[dict]]:
    rows = []
    for name, check in task.checks:
        try:
            ok, detail = check(workspace)
        except Exception as error:
            ok, detail = False, f"check crashed: {error}"
        rows.append({"name": name, "ok": bool(ok), "detail": str(detail)[:160]})
    return (sum(r["ok"] for r in rows) / len(rows) if rows else 0.0), rows


def runtime_check(task: Task, workspace: Path) -> str:
    """Open each HTML artifact in a headless browser and count load-time
    errors — the check that static scoring cannot do. Measured 2026-09-02:
    a page that passed nine static checks threw `togglePause is not
    defined` at load. Needs Playwright + Chromium; without them the answer
    is "not measured", never a silent pass. Recorded beside the score, not
    inside it, so scores stay comparable across machines."""
    pages = [workspace / rel for rel in task.artifacts if rel.endswith(".html") and (workspace / rel).exists()]
    if not pages:
        return ""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "not measured (pip install playwright && playwright install chromium)"
    notes = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for page_path in pages:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.goto(page_path.as_uri(), wait_until="load")
            page.wait_for_timeout(1500)                    # let animations start
            notes.append(f"{page_path.name}: {len(errors)} errors"
                         + (f" — {errors[0][:120]}" if errors else ""))
            page.close()
        browser.close()
    return "; ".join(notes)


def keep_artifacts(task: Task, workspace: Path, dest: Path) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    kept = []
    for rel in task.artifacts:
        src = workspace / rel
        if src.exists():
            target = dest / Path(rel).name
            shutil.copy2(src, target)
            kept.append(str(target.relative_to(RESULTS)))
    return kept


def place_setup(task: Task, workspace: Path) -> None:
    for rel, text in task.setup.items():
        path = workspace / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def clean_task_files(task: Task, workspace: Path) -> None:
    """Remove what the task put there and what it was asked to make — and
    nothing else (Seymour's workspace is a person's folder)."""
    for rel in list(task.setup) + task.artifacts:
        path = workspace / rel
        if path.is_file():
            path.unlink()
    for rel in task.setup:
        parent = (workspace / rel).parent
        if parent != workspace and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
    for junk in ("__pycache__", ".pytest_cache", "app/__pycache__"):
        shutil.rmtree(workspace / junk, ignore_errors=True)


# ------------------------------------------------------------- Seymour
async def run_seymour(task: Task, model_id: str) -> dict:
    """One discrete agent task through the live app."""
    clean_task_files(task, WORKSPACE)
    place_setup(task, WORKSPACE)
    started = time.monotonic()
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as c:
        created = (await c.post("/api/tasks", json={"goal": task.prompt, "session_id": ""})).json()
        task_id = created["id"]
        status = "timeout"
        while time.monotonic() - started < task.timeout_s:
            rows = [t for t in (await c.get("/api/tasks")).json()["tasks"] if t["id"] == task_id]
            if rows and rows[0]["status"] in ("done", "failed", "cancelled", "paused", "blocked"):
                status = rows[0]["status"]
                break
            await asyncio.sleep(4)
        else:
            await c.post(f"/api/tasks/{task_id}/cancel")
        journal = (await c.get(f"/api/tasks/{task_id}/journal")).json()
        result = next((t.get("result", "") for t in (await c.get("/api/tasks")).json()["tasks"]
                       if t["id"] == task_id), "")
    seconds = round(time.monotonic() - started, 1)
    value, rows = score(task, WORKSPACE)
    runtime = runtime_check(task, WORKSPACE)
    kept = keep_artifacts(task, WORKSPACE, RESULTS / f"compare-{LABEL}" / "seymour" / task.id)
    clean_task_files(task, WORKSPACE)
    return {"harness": "seymour", "task": task.id, "category": task.category,
            "status": status, "seconds": seconds, "score": round(value, 3),
            "tool_calls": sum(1 for s in journal if s["kind"] == "tool_call"),
            "checks": rows, "artifacts": kept, "runtime": runtime, "final": (result or "")[:400]}


# ------------------------------------------------------------- dsh
def run_dsh(task: Task, model_id: str) -> dict:
    """One unattended run of dsh's minimal agent in a fresh directory."""
    root = Path(f"/tmp/seymour-compare/{LABEL}/{task.id}")
    shutil.rmtree(root, ignore_errors=True)
    workspace = root / "ws"
    workspace.mkdir(parents=True)
    place_setup(task, workspace)
    env = {**os.environ, "DSH_HOME": str(root / "dsh-home")}
    (root / "dsh-home").mkdir(parents=True, exist_ok=True)
    # The SDK runner (evals/compare/dsh_agent.py) runs with the dsh venv's
    # interpreter; the agent's own shell gets Seymour's venv first on PATH
    # so `python` there has openpyxl / python-pptx too — parity of tools.
    cmd = [str(DSH_VENV / "bin" / "python"),
           str(Path(__file__).with_name("dsh_agent.py")),
           "--workspace", str(workspace), "--model", model_id,
           "--base-url", DSH_BASE_URL,
           "--base-url", f"{ENGINE}/v1", "--session-id", f"cmp-{task.id}",
           "--timeout", str(task.timeout_s), "--dsh-home", str(root / "dsh-home"),
           "--path", str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
           task.prompt]
    started = time.monotonic()
    status, final, calls = "failed", "", 0
    try:
        proc = subprocess.run(cmd, cwd=workspace, env=env, capture_output=True,
                              text=True, timeout=task.timeout_s + 60)
        try:
            record = json.loads(proc.stdout.strip().splitlines()[-1])
            status, final, calls = record["status"], record["final"], record["tool_events"]
        except (json.JSONDecodeError, IndexError, KeyError):
            final = (proc.stdout + "\n" + proc.stderr).strip()[-800:]
    except subprocess.TimeoutExpired:
        status, final = "timeout", "(timed out)"
    seconds = round(time.monotonic() - started, 1)
    value, rows = score(task, workspace)
    runtime = runtime_check(task, workspace)
    kept = keep_artifacts(task, workspace, RESULTS / f"compare-{LABEL}" / "dsh" / task.id)
    return {"harness": "dsh", "task": task.id, "category": task.category,
            "status": status, "seconds": seconds, "score": round(value, 3),
            "tool_calls": calls, "checks": rows, "artifacts": kept, "runtime": runtime, "final": final[:400]}


# ------------------------------------------------------------- report
ENGINE_NAME = "llama-server"           # replaced by /api/status's engine_name at run time


def report(records: list[dict], model_id: str, path: Path) -> str:
    by = {(r["harness"], r["task"]): r for r in records}
    harnesses = sorted({r["harness"] for r in records})
    lines = [f"# Harness comparison — {LABEL} — {date.today()}", "",
             f"Model: `{model_id}` on {ENGINE_NAME} (thinking on for both). "
             f"Score = fraction of programmatic checks passed.", "",
             "| task | category | " + " | ".join(f"{h} score" for h in harnesses)
             + " | " + " | ".join(f"{h} time" for h in harnesses)
             + " | " + " | ".join(f"{h} tools" for h in harnesses) + " |",
             "|---|---|" + "---|" * (3 * len(harnesses))]
    for task in TASKS:
        cells = [task.id, task.category]
        for kind in ("score", "seconds", "tool_calls"):
            for h in harnesses:
                r = by.get((h, task.id))
                if r is None:
                    cells.append("—")
                elif kind == "score":
                    cells.append(f"{r['score']:.2f}" + ("" if r["status"] == "done" else f" ({r['status']})"))
                elif kind == "seconds":
                    cells.append(f"{r['seconds']}s")
                else:
                    cells.append(str(r["tool_calls"]))
        lines.append("| " + " | ".join(cells) + " |")
    for h in harnesses:
        rows = [r for r in records if r["harness"] == h]
        if rows:
            lines.append(f"\n**{h} mean score: {sum(r['score'] for r in rows) / len(rows):.2f}** "
                         f"over {len(rows)} tasks, {sum(r['seconds'] for r in rows):.0f}s total.")
    lines.append("\n## Per-check detail\n")
    for r in records:
        lines.append(f"### {r['harness']} · {r['task']} — {r['score']:.2f} ({r['status']}, {r['seconds']}s, {r['tool_calls']} tools)")
        for c in r["checks"]:
            lines.append(f"- {'✅' if c['ok'] else '❌'} {c['name']} — {c['detail']}")
        if r.get("runtime"):
            lines.append(f"- runtime (headless load): {r['runtime']}")
        for a in r["artifacts"]:
            lines.append(f"- artifact: `results/{a}`")
        if r["final"]:
            lines.append(f"- final: {r['final'][:200].replace(chr(10), ' ')}")
        lines.append("")
    text = "\n".join(lines)
    path.write_text(text, encoding="utf-8")
    return text


LABEL = "run"


async def main() -> None:
    global LABEL, DSH_BASE_URL, ENGINE_NAME
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="run")
    parser.add_argument("--harness", default="seymour,dsh")
    parser.add_argument("--only", default="")
    parser.add_argument("--dsh-base-url", default=DSH_BASE_URL,
                        help="the OpenAI-compatible endpoint dsh talks to (8080 llama, 8082 MLX)")
    args = parser.parse_args()
    LABEL = args.label
    DSH_BASE_URL = args.dsh_base_url
    harnesses = [h.strip() for h in args.harness.split(",") if h.strip()]
    tasks = [t for t in TASKS if not args.only or t.id in args.only.split(",")]

    status = httpx.get(f"{BASE}/api/status", timeout=10).json()
    ENGINE_NAME = (status.get("capabilities") or {}).get("engine_name") or ENGINE_NAME
    if not status.get("capabilities"):
        raise SystemExit("load a model first (Models tab)")
    model_id = status["capabilities"]["model_id"]
    # Parity: thinking follows each run kind's default (agent steps: on).
    httpx.post(f"{BASE}/api/inference", json={"thinking": "auto"}, timeout=10)

    RESULTS.mkdir(exist_ok=True)
    records: list[dict] = []
    print(f"comparison '{LABEL}': {len(tasks)} tasks × {harnesses} on {model_id}")
    for task in tasks:
        for harness in harnesses:
            print(f"  {harness:8} {task.id:24} …", end="", flush=True)
            record = await run_seymour(task, model_id) if harness == "seymour" else \
                await asyncio.to_thread(run_dsh, task, model_id)
            records.append(record)
            print(f" {record['score']:.2f}  {record['seconds']:6.1f}s  {record['tool_calls']} tools  [{record['status']}]")
            (RESULTS / f"compare-{LABEL}-{date.today()}.json").write_text(
                json.dumps({"label": LABEL, "model": model_id, "records": records}, indent=1))
    text = report(records, model_id, RESULTS / f"compare-{LABEL}-{date.today()}.md")
    print("\n" + text.split("\n## Per-check")[0])


if __name__ == "__main__":
    asyncio.run(main())
