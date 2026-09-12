"""Deep research's HTTP surface: start a run, poll it, cancel it.

Live progress arrives over /api/events (topic "research"); the GET here is
the catch-up call when a view opens mid-run.
"""

import asyncio
import json
import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from seymour.config import settings
from seymour.research import research

router = APIRouter(prefix="/api/research")


class NewResearch(BaseModel):
    """What to research."""

    question: str


@router.post("")
async def start(body: NewResearch):
    """Begin a research run (Tier 2 — yields to chat, outranks the agent)."""
    if not body.question.strip():
        raise HTTPException(422, "question must not be empty")
    # Setup mode: research needs a model too.
    from seymour import runtime
    if runtime.scheduler is None:
        raise HTTPException(
            503, "No model is loaded yet — download or activate one in the "
                 "Models tab.")
    job_id = research.start(body.question)
    return {"id": job_id}


@router.get("")
async def list_runs():
    """Every run, newest first — the organizer tab is a view of PAST AND
    CURRENT work, so live in-memory jobs are merged with the sidecar
    JSONs finished runs leave in the workspace (in-memory wins when both
    exist; a restart clears memory but never the archive)."""
    jobs = sorted(research.jobs.values(), key=lambda j: j.started, reverse=True)
    live = [
        {
            "id": j.id, "question": j.question, "status": j.status,
            "phase": j.phase, "round": j.round,
            "sources": len(j.sources), "result_path": j.result_path,
            "session_id": j.session_id, "archived": False,
        }
        for j in jobs
    ]
    live_questions = {j.question for j in jobs}

    def read_archive() -> list[dict]:
        """The workspace's research-*.json sidecars, newest first.

        The workspace is agent- and user-writable, so any one file may
        be malformed in any way — each file's ENTIRE row build sits in
        its own try, and a bad sidecar hides itself rather than 500ing
        the whole organizer (live rows included).
        """
        rows: list[dict] = []

        def mtime(p) -> float:
            try:
                return p.stat().st_mtime
            except OSError:
                return 0.0                 # vanished mid-listing: sort last

        paths = sorted(settings.workspace_dir.glob("research-*.json"),
                       key=mtime, reverse=True)
        for path in paths[:50]:            # bounded: an organizer, not an archive browser
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue               # valid JSON, wrong shape
                if data.get("question") in live_questions:
                    continue               # the live row already tells it better
                markdown = path.with_suffix(".md")
                stats = data.get("stats")
                sources = data.get("sources")
                rows.append({
                    # The filename stem is a stable id for expand/collapse
                    # in the UI (archived rows carry their report inline).
                    "id": f"archive:{path.stem}",
                    "question": str(data.get("question", path.stem)),
                    "status": str(data.get("status", "done")),
                    "phase": "",
                    "round": (stats or {}).get("rounds", 0)
                             if isinstance(stats, dict) else 0,
                    "sources": len(sources) if isinstance(sources, list) else 0,
                    "result_path": str(markdown) if markdown.exists() else "",
                    "session_id": str(data.get("session_id", "")),
                    "archived": True,
                    "report": str(data.get("report", "")),
                })
            except (OSError, json.JSONDecodeError, AttributeError,
                    TypeError, ValueError):
                continue                   # a corrupt sidecar hides itself
        return rows

    rows = live + await asyncio.to_thread(read_archive)

    # Which originating conversations still exist (bug 4.3): rows whose
    # chat is gone keep their report but disable "Open chat" honestly.
    def check_sessions() -> set:
        from seymour.db import ChatSession, SessionLocal
        wanted = {r["session_id"] for r in rows if r["session_id"]}
        if not wanted:
            return set()
        with SessionLocal() as db:
            return {row[0] for row in
                    db.query(ChatSession.id).filter(ChatSession.id.in_(wanted))}

    existing = await asyncio.to_thread(check_sessions)
    for row in rows:
        row["session_exists"] = row["session_id"] in existing
    return rows


@router.get("/download/{stem}")
async def download(stem: str):
    """Serve a saved report's markdown file as a download (the viewer's
    export button). The stem is validated to the exact shape _save
    writes and resolved strictly inside the workspace — this endpoint
    can never serve an arbitrary host path."""
    if not re.fullmatch(r"research-[a-z0-9-]{1,80}", stem):
        raise HTTPException(404, "no such report")
    path = (settings.workspace_dir / f"{stem}.md").resolve()
    if (settings.workspace_dir.resolve() not in path.parents
            or not path.exists()):
        raise HTTPException(404, "no such report")
    return FileResponse(path, media_type="text/markdown",
                        filename=path.name)


@router.get("/{job_id}")
async def status(job_id: str):
    """One run's full state, including the evolving draft."""
    job = research.get(job_id)
    if job is None:
        raise HTTPException(404, "no such research run")
    return {
        "id": job.id, "question": job.question, "status": job.status,
        "phase": job.phase, "round": job.round, "report": job.report,
        "sources": job.sources, "result_path": job.result_path,
        "error": job.error,
    }


@router.post("/{job_id}/cancel")
async def cancel(job_id: str):
    """Stop a run; the draft so far is saved as its result."""
    if research.get(job_id) is None:
        raise HTTPException(404, "no such research run")
    await research.cancel(job_id)
    return {"status": "cancelled"}
