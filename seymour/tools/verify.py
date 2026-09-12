"""auto_check: the harness verifies what the model just wrote, every time.

Nick's rule (2026-09-03): "Seymour's goal would be to give a final,
known working product." A skill can ASK the model to verify; a run that
skips the step still ends with a page that throws. So verification is
the harness's job: after every write-tier file tool, the file's kind
picks a check, the check runs, and its report is appended to the tool
result the model reads next — and the run is not allowed to finish while
a page it wrote still fails (run_executor's repair guard).

    .html/.htm   check_page — loads it in the person's browser: console
                 errors, missing ids, animation frames, canvas coverage
    .py          python -m py_compile (the model's sandbox)
    .js/.mjs     node --check (when node exists)
    .json        json.loads
    anything else: no check (None) — nothing is claimed.

A check that cannot run says "not measured", never "PASS".
"""

import json
import re
from pathlib import Path

from seymour.tools import paths

CHECKABLE = (".html", ".htm", ".py", ".js", ".mjs", ".json")


def _verdict_of(report: str) -> str:
    m = re.search(r"verdict:\s*(PASS|FIX NEEDED)", report)
    if m:
        return m.group(1)
    if "could not run" in report or "timed out" in report or "not installed" in report:
        return "not measured"
    return "FIX NEEDED" if report.startswith("Error") else "PASS"


async def auto_check(path: str) -> dict | None:
    """Run the check for this file's kind. Returns
    {"kind", "verdict": PASS|FIX NEEDED|not measured, "report": str} or None."""
    try:
        target = paths.resolve(path)
    except ValueError:
        return None
    suffix = target.suffix.lower()
    if suffix not in CHECKABLE or not target.is_file():
        return None
    from seymour import tools                      # late: the registry imports this module's family
    if suffix in (".html", ".htm"):
        report = await tools.execute("check_page", {"path": path, "seconds": 3})
        return {"kind": "check_page", "verdict": _verdict_of(report), "report": report}
    if suffix == ".json":
        try:
            json.loads(target.read_text(encoding="utf-8", errors="replace"))
            return {"kind": "json", "verdict": "PASS", "report": f"[auto-check {path}] valid JSON."}
        except ValueError as error:
            return {"kind": "json", "verdict": "FIX NEEDED", "report": f"[auto-check {path}] invalid JSON: {error}"}
    if suffix == ".py":
        rel = paths.display(target)
        out = await tools.execute("run_command", {"command": f"python -m py_compile {json.dumps(rel)} && echo COMPILED", "timeout_s": 30})
        ok = "COMPILED" in out and "exit code 0" in out
        return {"kind": "py_compile", "verdict": "PASS" if ok else "FIX NEEDED",
                "report": f"[auto-check {path}] " + ("compiles." if ok else "does not compile:\n" + out[-1200:])}
    if suffix in (".js", ".mjs"):
        rel = paths.display(target)
        out = await tools.execute("run_command", {"command": f"node --check {json.dumps(rel)} && echo SYNTAX_OK", "timeout_s": 30})
        if "command not found" in out or "No such file" in out and "node" in out:
            return {"kind": "node_check", "verdict": "not measured", "report": f"[auto-check {path}] node is not available."}
        ok = "SYNTAX_OK" in out and "exit code 0" in out
        return {"kind": "node_check", "verdict": "PASS" if ok else "FIX NEEDED",
                "report": f"[auto-check {path}] " + ("valid JavaScript." if ok else "syntax error:\n" + out[-1200:])}
    return None


def summarize(check: dict | None) -> str:
    """One line for a card or a log."""
    if not check:
        return ""
    return f"{check['kind']}: {check['verdict']}"
