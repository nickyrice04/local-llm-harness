"""Runs: the trace surface (stage 4/5's headline feature).

Every run's whole story is already in its append-only event log — this
router just reads it. Nothing here computes or re-derives anything: if
a fact isn't in the log, the trace doesn't claim it.
"""

import asyncio
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from seymour.db import ChatSession, Run, RunEvent, SessionLocal

router = APIRouter(prefix="/api/runs")


@router.get("")
async def list_runs(limit: int = 100):
    """Recent runs, newest first — the trace tab's index."""
    def read() -> list[dict]:
        with SessionLocal() as db:
            runs = (db.query(Run).order_by(Run.created_at.desc())
                    .limit(min(limit, 500)).all())
            wanted = {r.session_id for r in runs}
            existing = ({row[0] for row in db.query(ChatSession.id)
                         .filter(ChatSession.id.in_(wanted))}
                        if wanted else set())
            rows = []
            for run in runs:
                counts = {}
                for event in run.events:
                    counts[event.type] = counts.get(event.type, 0) + 1
                rows.append({
                    "id": run.id, "session_id": run.session_id,
                    "session_exists": run.session_id in existing,
                    "preset": run.preset, "status": run.status,
                    "stats": json.loads(run.stats) if run.stats else {},
                    "created_at": run.created_at.isoformat(),
                    "tool_calls": counts.get("tool_call", 0),
                    "model_calls": counts.get("model_call", 0),
                    "events": sum(counts.values()),
                })
            return rows
    return await asyncio.to_thread(read)


@router.get("/{run_id}")
async def get_run(run_id: str):
    """One run's FULL event log — the trace view reads this directly."""
    def read() -> dict:
        with SessionLocal() as db:
            run = db.get(Run, run_id)
            if run is None:
                raise HTTPException(404, "no such run")
            return {
                "id": run.id, "session_id": run.session_id,
                "preset": run.preset, "status": run.status,
                "stats": json.loads(run.stats) if run.stats else {},
                "created_at": run.created_at.isoformat(),
                "events": [
                    {"seq": e.seq, "type": e.type,
                     "at": e.created_at.isoformat(),
                     "data": json.loads(e.data) if e.data else {}}
                    for e in run.events
                ],
            }
    return await asyncio.to_thread(read)


class Decision(BaseModel):
    """The person's answer to a run's approval request."""

    allow: bool


@router.post("/{run_id}/approve")
async def approve(run_id: str, body: Decision):
    """Answer a paused run's approval request (the inline Allow/Not this
    time buttons). A stale click — the run already moved on or timed
    out — is reported plainly, never treated as an error."""
    from seymour.run_executor import decide
    if not decide(run_id, body.allow):
        return {"answered": False, "note": "that request is no longer waiting"}
    return {"answered": True, "allow": body.allow}


@router.get("/{run_id}/export")
async def export_run(run_id: str):
    """The run as JSONL — one event per line (dsh's session-log export:
    the whole trace, portable, greppable, diffable)."""
    def read() -> str:
        with SessionLocal() as db:
            run = db.get(Run, run_id)
            if run is None:
                raise HTTPException(404, "no such run")
            lines = [json.dumps({"type": "run", "id": run.id,
                                 "preset": run.preset, "status": run.status,
                                 "session_id": run.session_id,
                                 "stats": json.loads(run.stats) if run.stats else {},
                                 "created_at": run.created_at.isoformat()})]
            for event in run.events:
                lines.append(json.dumps({
                    "seq": event.seq, "type": event.type,
                    "at": event.created_at.isoformat(),
                    "data": json.loads(event.data) if event.data else {}}))
            return "\n".join(lines) + "\n"
    body = await asyncio.to_thread(read)
    return PlainTextResponse(
        body, media_type="application/x-ndjson",
        headers={"Content-Disposition":
                 f'attachment; filename="run-{run_id[:8]}.jsonl"'})
