"""Build judging packets from rendered eval artifacts, and fold verdicts back.

    packet.py --label <label>        packets for every task with renders in
                                     evals/results/compare-<label>/
    packet.py --calibration          packets for evals/render/calibration/
    packet.py --collect --label X    read verdict.json files, un-blind, summarize

The judge itself is a subagent run by the person/agent driving the evals
(see README.md); this script only decides WHAT it sees — randomized A/B,
neutral filenames, the prompt, the rubric, the anchors.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "evals" / "compare"))

RESULTS = REPO / "evals" / "results"
CALIBRATION = REPO / "evals" / "render" / "calibration"
PROMPT = (REPO / "evals" / "judge" / "prompt.md").read_text(encoding="utf-8")

RUBRICS = {
    "deck": ["Visual consistency across slides (one palette, one type scale, aligned grid)",
             "No overflow, no empty placeholders, nothing cut off",
             "One idea per slide; titles state the point; text is readable at a glance",
             "The chart (if any) is built from the data, labelled, and readable",
             "Would a person present this?"],
    "page": ["Layout and visual polish at desktop width (spacing, alignment, colour)",
             "Holds up on the laptop and phone viewports (no overflow, no unreadable text)",
             "The interactions asked for visibly work (compare the after-interaction images)",
             "No console errors; nothing visibly broken",
             "Would a person use this?"],
    "workbook": ["The summary sheet reads clearly (headers, formats, alignment, widths)",
                 "Numbers are formatted (currency, thousands, dates) and totals are visible",
                 "The chart (if any) is labelled and matches the data",
                 "Nothing is cut off, #ERROR-ed or empty where data is expected",
                 "Would a person send this to a colleague?"],
    "document": ["Real headings and structure; the page reads as a document, not a dump",
                 "Tables and figures are placed, captioned and readable",
                 "Nothing cut off or overflowing the page",
                 "Would a person send this?"],
    "chart": ["The title states the finding; axes are labelled with units",
              "Readable at a glance (no clutter, sensible scale, legend only when needed)",
              "Nothing cut off; the PNG is complete",
              "Would a person put this in a report?"],
}


def rubric_for(kind: str) -> str:
    return "\n".join(f"{i}. {c}" for i, c in enumerate(RUBRICS.get(kind, RUBRICS["page"]), 1))


def _copy_images(images: list[str], dest: Path) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for index, image in enumerate(sorted(images), 1):
        src = Path(image)
        if not src.exists():
            continue
        target = dest / f"{index:02d}{src.suffix.lower()}"
        shutil.copy2(src, target)
        out.append(str(target.relative_to(dest.parent)))
    return out


def build_packet(out: Path, prompt: str, kind: str, artifacts: dict[str, dict], seed: int | None = None) -> dict:
    """artifacts: {"seymour": {"images": [...], "console": text}, "dsh": {...}} (one or two)."""
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    names = list(artifacts)
    rng = random.Random(seed)
    rng.shuffle(names)
    letters = ["A", "B"][: len(names)]
    mapping = dict(zip(letters, names))
    (out / "mapping.json").write_text(json.dumps(mapping, indent=1))     # never shown to the judge
    described = []
    packet = {"prompt": prompt, "kind": kind, "rubric": RUBRICS.get(kind, RUBRICS["page"]), "artifacts": {}}
    for letter, name in mapping.items():
        images = _copy_images(artifacts[name].get("images", []), out / letter)
        console = (artifacts[name].get("console") or "").strip()
        interaction = (artifacts[name].get("interaction") or "").strip()
        packet["artifacts"][letter] = {"images": images, "console": console[:4000], "interaction": interaction[:3000]}
        block = f"### Artifact {letter}\nImages (look at every one): " + ", ".join(images or ["(none — the artifact did not render)"])
        if console:
            block += f"\nConsole log:\n```\n{console[:2000]}\n```"
        if interaction:
            block += f"\nInteraction results (✔ passed, ✘ failed):\n```\n{interaction[:2000]}\n```"
        described.append(block)
    (out / "packet.json").write_text(json.dumps(packet, indent=1))
    (out / "prompt.md").write_text(PROMPT.replace("{{prompt}}", prompt).replace("{{rubric}}", rubric_for(kind))
                                   .replace("{{artifacts}}", "\n\n".join(described)))
    return mapping


def build_from_run(label: str) -> list[Path]:
    """Packets for every task of a comparison run that has renders."""
    from tasks import TASKS
    root = RESULTS / f"compare-{label}"
    built = []
    for task in TASKS:
        if not getattr(task, "render", None):
            continue
        artifacts = {}
        for harness in ("seymour", "dsh"):
            render_dir = root / harness / task.id / "render"
            if not render_dir.exists():
                continue
            images = [str(p) for p in sorted(render_dir.rglob("*.png")) if p.name != "contact.png" or True]
            console = (render_dir / "console.log").read_text(encoding="utf-8") if (render_dir / "console.log").exists() else ""
            interaction = (render_dir / "interaction.txt").read_text(encoding="utf-8") if (render_dir / "interaction.txt").exists() else ""
            artifacts[harness] = {"images": images, "console": console, "interaction": interaction}
        if not artifacts:
            continue
        out = RESULTS / f"judge-{label}" / task.id
        build_packet(out, task.prompt, task.render.get("kind", "page"), artifacts)
        built.append(out)
    return built


def build_calibration(label: str = "calibration") -> list[Path]:
    """Packets for the hand-scored references (single artifacts)."""
    manifest = json.loads((CALIBRATION / "calibration.json").read_text())
    built = []
    for item in manifest:
        render_dir = CALIBRATION / item["id"] / "render"
        images = [str(p) for p in sorted(render_dir.rglob("*.png"))]
        out = RESULTS / f"judge-{label}" / item["id"]
        build_packet(out, item["prompt"], item["kind"], {"reference": {"images": images, "console": ""}}, seed=0)
        built.append(out)
    return built


def collect(label: str) -> dict:
    """Un-blind every verdict.json under judge-<label> and summarize."""
    root = RESULTS / f"judge-{label}"
    summary: dict = {"label": label, "tasks": {}}
    for task_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        verdict_path = task_dir / "verdict.json"
        mapping_path = task_dir / "mapping.json"
        if not verdict_path.exists() or not mapping_path.exists():
            continue
        verdict = json.loads(verdict_path.read_text())
        mapping = json.loads(mapping_path.read_text())
        row: dict = {}
        for letter, name in mapping.items():
            art = (verdict.get("artifacts") or {}).get(letter) or {}
            row[name] = {"overall": art.get("overall"), "fix": art.get("highest_leverage_fix"),
                         "criteria": art.get("criteria")}
        winner = verdict.get("winner")
        row["winner"] = mapping.get(winner, winner) if winner in ("A", "B") else winner
        row["confidence"] = verdict.get("confidence")
        summary["tasks"][task_dir.name] = row
    (RESULTS / f"judge-{label}.json").write_text(json.dumps(summary, indent=1))
    return summary


def drift(label: str = "calibration") -> list[dict]:
    """Judge scores on the references vs their hand-fixed scores."""
    manifest = {i["id"]: i for i in json.loads((CALIBRATION / "calibration.json").read_text())}
    collected = collect(label)
    rows = []
    for task_id, row in collected["tasks"].items():
        expected = manifest.get(task_id, {}).get("score")
        got = (row.get("reference") or {}).get("overall")
        rows.append({"id": task_id, "expected": expected, "judged": got,
                     "drift": (got - expected) if isinstance(got, (int, float)) and isinstance(expected, (int, float)) else None})
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="")
    parser.add_argument("--calibration", action="store_true")
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--drift", action="store_true")
    args = parser.parse_args()
    if args.calibration:
        for p in build_calibration():
            print("packet:", p)
    elif args.drift:
        for row in drift():
            print(row)
    elif args.collect:
        print(json.dumps(collect(args.label), indent=1))
    else:
        for p in build_from_run(args.label):
            print("packet:", p)
