"""Re-score a finished comparison from its SAVED artifacts.

For when a check was wrong (the csv total, 2026-09-02) or a check is
added: the artifacts copied per harness/task are the outputs; running the
current checks over them corrects the JSON and regenerates the report
without re-running the models. Only tasks whose artifact list covers
every file their checks read can be rescored — pass --only for those;
checks that need the whole workspace (pytest) keep their recorded result.

    .venv/bin/python evals/compare/rescore.py --label first
"""

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run as runner                     # noqa: E402  (report + RESULTS)
from tasks import TASKS                   # noqa: E402
from tasks_v1 import TASKS as TASKS_V1    # noqa: E402  (labels from before 2026-09-11)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--only", default="",
                        help="comma-separated task ids; others keep their recorded scores")
    args = parser.parse_args()
    only = {t for t in args.only.split(",") if t}
    runner.LABEL = args.label
    path = next(p for p in sorted(runner.RESULTS.glob(f"compare-{args.label}-*.json")))
    data = json.loads(path.read_text())
    # Labels from before 2026-09-11 were scored on the first set.
    by_id = {t.id: t for t in (TASKS + TASKS_V1)}
    for record in data["records"]:
        task = by_id.get(record["task"])
        if task is None or not record["artifacts"] or (only and task.id not in only):
            continue
        # Rebuild a workspace holding just the artifacts, at their paths.
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            for rel in task.artifacts:
                src = runner.RESULTS / f"compare-{args.label}" / record["harness"] / task.id / Path(rel).name
                if src.exists():
                    (ws / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, ws / rel)
            old = {c["name"]: c for c in record["checks"]}
            rows = []
            for name, check in task.checks:
                if name == "tests pass" or name.startswith("test_"):
                    rows.append(old.get(name, {"name": name, "ok": False, "detail": "not re-scorable"}))
                    continue
                try:
                    ok, detail = check(ws)
                except Exception as error:
                    ok, detail = False, f"check crashed: {error}"
                rows.append({"name": name, "ok": bool(ok), "detail": str(detail)[:160]})
            record["checks"] = rows
            record["score"] = round(sum(r["ok"] for r in rows) / len(rows), 3) if rows else 0.0
    path.write_text(json.dumps(data, indent=1))
    text = runner.report(data["records"], data["model"], path.with_suffix(".md"))
    print(text.split("\n## Per-check")[0])


if __name__ == "__main__":
    main()
