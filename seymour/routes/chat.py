"""Chat: the command center. One composer, three kinds of work.

The mode field decides what a message BECOMES:

    "chat"     → answered here, streaming (Tier 1 — a human is watching).
                 The model may call web_search/fetch_page mid-answer when
                 the question needs the live internet (bounded rounds).
    "agent"    → becomes a DISCRETE agent task (Tier 2) — "find the sheet,
                 combine it, highlight the figures". Tracked in the Agent
                 tab; a card in the conversation links to it.
    "research" → becomes a deep-research run (Tier 2). Tracked in the
                 Research tab, same pattern.

Attachments ride as upload ids: documents arrive as guard-wrapped text,
images go to the model as data URIs — IF the loaded model actually has
vision (measured by the handshake, never assumed from the name).

The SSE contract with the browser:

    data: {"session_id": …}     first — names a new conversation
    data: {"tool": {…}}         the model is using a tool ("searching…")
    data: {"delta": …}          per visible token fragment
    data: {"stats": {…}}        the reply's telemetry, once, before DONE
    data: [DONE]                done
    data: {"error": …}          on failure
"""

import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from seymour import runtime
from seymour.agent.discrete import discrete
from seymour.db import ChatSession, Message, SessionLocal, Upload, utcnow
from seymour.engine.adapter import GenerationRequest
from seymour.events import bus
from seymour import inference
from seymour.guard import untrusted_context_message
from seymour.memory.store import format_for_prompt, increment_uses, retrieve
from seymour.persona.soul import get_soul
from seymour.prompts import load
from seymour.research import research
from seymour.run_executor import CHAT_POLICY, render_catalog_for, stream_chat_run
from seymour.scheduler.tiers import Tier

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

class ChatRequest(BaseModel):
    """One composer submit. Pydantic validates the shape (missing message
    = clear 422); mode and attachments have safe defaults."""

    session_id: str | None = None
    message: str
    mode: str = "chat"                    # "chat" | "agent" | "research"
    attachments: list[str] = []           # Upload ids from POST /api/upload
    # "Yes — and bring context": when a task mode is chosen mid-way
    # through a DEDICATED plain chat, the UI offers to spin the task off
    # into a NEW conversation seeded with this one's recent turns. This
    # names the conversation to condense from (session_id stays None —
    # the task gets its own conversation).
    context_from: str | None = None
    # This message's inference overrides (the composer's quick toggles —
    # e.g. {"thinking": "on"}); they layer on the saved settings for this
    # run only and are recorded in the run's trace.
    inference: dict | None = None


async def _generate_title(session_id: str, first_message: str) -> None:
    """Give a new conversation a real title — quietly, at Tier 3.

    Fire-and-forget: a failed title costs nothing (the fallback truncation
    is already in place).
    """
    request = GenerationRequest(
        messages=[{"role": "user",
                   "content": load("title", message=first_message[:500])}],
        max_tokens=24,
        temperature=0.1,
    )
    try:
        title = await runtime.scheduler.complete(
            Tier.BACKGROUND_AGENT, request, label="chat:title")
        title = title.strip().strip('"')[:80]
        if title:
            with SessionLocal() as db:
                session = db.get(ChatSession, session_id)
                if session:
                    session.title = title
                    db.commit()
            bus.publish("chat", "title", session_id=session_id, title=title)
    except Exception:
        logger.debug("title generation skipped", exc_info=True)


def _vision_reason() -> str:
    """WHY images can't be seen right now — the exact, actionable truth.

    Three distinguishable situations, diagnosed from the engine's REAL
    state (never guessed from the model's name):
      - a projector file exists on disk but the adopted, hand-launched
        server never loaded it → reloading through Seymour fixes it;
      - no projector file on disk at all → it needs downloading first
        (the weights alone are text-only, whatever the family's cards say);
      - anything else → the plain fact, unadorned.
    """
    engine = runtime.engine
    model_path = getattr(engine, "model_path", None)
    mmproj = (sorted(model_path.parent.glob("*mmproj*.gguf"))
              if model_path else [])
    if mmproj and getattr(engine, "external", False):
        return ("The running llama-server was started by hand without its "
                f"vision adapter. Unload and Load the model in the Models "
                f"tab so Seymour relaunches it with {mmproj[0].name}.")
    if not mmproj:
        name = model_path.name if model_path else "the loaded model"
        return (f"No vision projector (an *mmproj*.gguf file) exists next "
                f"to {name} — these weights alone are text-only. Download "
                f"the matching mmproj file into the same folder, then "
                f"reload the model.")
    return "The engine reports no vision projector loaded."


def _load_attachments(ids: list[str]) -> list[Upload]:
    """Resolve attachment ids to rows (unknown ids are simply skipped —
    the message still sends; a vanished upload must not block a chat)."""
    if not ids:
        return []
    with SessionLocal() as db:
        rows = db.query(Upload).filter(Upload.id.in_(ids[:8])).all()
    # Preserve the composer's order.
    by_id = {r.id: r for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def _image_content(uploads: list[Upload], message: str) -> list[dict]:
    """Build the OpenAI-style content array for a vision request: the text
    plus each image as a base64 data URI (the wire format llama-server's
    multimodal endpoint expects — never file paths)."""
    import base64
    from seymour.config import settings
    content: list[dict] = [{"type": "text", "text": message}]
    for upload in uploads:
        path = settings.uploads_dir / upload.filename
        try:
            encoded = base64.b64encode(path.read_bytes()).decode()
        except OSError:
            continue                       # file vanished: skip, don't crash
        mime = upload.mime or "image/png"
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{encoded}"}})
    return content


@router.post("/chat")
async def chat(req: ChatRequest):
    """The main endpoint: route by mode, then persist → retrieve → stream."""
    # Setup mode: no model loaded yet → an honest 503, not a crash.
    if runtime.scheduler is None:
        raise HTTPException(
            503, "No model is loaded yet — download or activate one in the "
                 "Models tab.")

    uploads = _load_attachments(req.attachments)
    # The message as HISTORY sees it: attachment names become part of the
    # text so reopening the conversation shows what was attached.
    display_message = req.message
    for upload in uploads:
        display_message += f"\n[attached: {upload.original_name}]"

    # --- 1. Find or create the conversation; save the user's turn FIRST ---
    # (before anything slow — a crash must not lose their message).
    def persist_user_turn() -> tuple[str, bool, list[dict]]:
        """All of turn-setup's DB work in one function so it can run in a
        worker thread — this read walks the WHOLE conversation, and on
        the event-loop thread it would freeze every live token stream."""
        with SessionLocal() as db:
            if req.session_id:
                session = db.get(ChatSession, req.session_id)
                if session is None:
                    raise HTTPException(404, "session not found")
                is_new = False
            else:
                session = ChatSession(
                    id=str(uuid.uuid4()),
                    # Truncation as a placeholder; the real title arrives async.
                    title=req.message[:60],
                )
                db.add(session)
                is_new = True
            db.add(Message(session_id=session.id, role="user",
                           content=display_message))
            # Touch the activity clock by hand: adding a Message row never
            # UPDATEs the session row, so without this the sidebar's
            # "newest activity first" ordering froze at creation time.
            session.updated_at = utcnow()
            db.commit()
            history = [
                {"role": m.role, "content": m.content}
                for m in db.get(ChatSession, session.id).messages
            ]
            return session.id, is_new, history

    session_id, is_new, history = await asyncio.to_thread(persist_user_turn)

    # --- 2. The agent / research modes hand the work to their subsystem ----
    # The conversation is where the work is WATCHED: progress streams into
    # it live, and the outcome lands in it as a message. The organizer
    # tabs merely organize.
    if req.mode in ("agent", "research"):
        # "Bring context": condense the originating conversation's recent
        # turns so the new task-conversation starts informed.
        context = ""
        if req.context_from:
            def read_context() -> str:
                with SessionLocal() as db:
                    source = db.get(ChatSession, req.context_from)
                    if source is None:
                        return ""       # vanished source: just skip context
                    return "\n".join(
                        f"[{m.role}] {m.content[:400]}"
                        for m in source.messages[-10:])
            context = await asyncio.to_thread(read_context)
        if req.mode == "agent":
            goal = req.message
            if context:
                goal += ("\n\nContext from the conversation this task was "
                         "spun off from:\n" + context)
            started = await discrete.create(goal, session_id)
            note = "Started an agent task — its progress streams here."
            job = {"kind": "agent", "id": started["id"]}
        else:
            job_id = research.start(req.message, session_id, context=context)
            note = "Started deep research — its progress streams here."
            job = {"kind": "research", "id": job_id}
        def persist_note() -> None:
            with SessionLocal() as db:
                db.add(Message(session_id=session_id, role="assistant",
                               content=note))
                # The chat REMEMBERS what kind it is (the sidebar symbol)
                # and which job it spawned (the live-in-a-slot spinner).
                session = db.get(ChatSession, session_id)
                if session:
                    if is_new:
                        session.kind = req.mode
                    session.job_id = job["id"]
                db.commit()
        await asyncio.to_thread(persist_note)
        if is_new:
            asyncio.create_task(_generate_title(session_id, req.message))
        return {"session_id": session_id, "job": job, "note": note}

    # --- 3. Plain chat: assemble the prompt with cache-friendly ordering ---
    # System prompt: soul + chat rules (incl. the tool protocol). STABLE
    # across the session's turns — that is what lets llama.cpp's
    # slot-continuation cache skip re-reading the conversation every turn.
    # The system prompt now carries the SCOPE-FILTERED tool catalog —
    # chat has the agent's tools (stage 4's convergence), and the model,
    # not a mode button, decides which ones an input needs.
    messages: list[dict] = [{"role": "system",
                             "content": load("chat_system", soul=get_soul(),
                                             tools=render_catalog_for(CHAT_POLICY))}]
    # All prior turns go in unchanged (the cached prefix)… within the
    # HISTORY BUDGET: the oldest turns fall off first when a conversation
    # outgrows it (the per-conversation "context size" — the engine's KV
    # pool is a separate, load-time setting). Trimming from the front
    # keeps the cached prefix stable for every turn that stays.
    inf = inference.current(req.inference)
    messages += inference.budget_history(history[:-1], inf.history_tokens)
    # …then anything that CHANGES per turn, just before the latest turn:
    # relevant memories and attached documents, guard-wrapped as untrusted.
    # The REAL date rides in the changing-tail section (never the system
    # prompt — the cache rule). Without it the model invents a date from
    # its training data and reasons itself out of searching ("it's
    # February, the tournament hasn't happened yet" — an observed miss).
    from datetime import date
    messages.append({"role": "user", "content": (
        f"(Context: today's date is {date.today().strftime('%B %d, %Y')}. "
        f"Your training data ends well before this — for anything that "
        f"happened since, search rather than guess.)")})
    memories = await retrieve(req.message)
    if memories:
        messages.append(untrusted_context_message(
            "seymour memory", format_for_prompt(memories)))
        # Count the INJECTION (the honest "12×" badge in the Memory tab —
        # retrieval alone doesn't count; reaching the prompt does).
        await asyncio.to_thread(
            increment_uses, [m["id"] for m in memories])
    documents = [u for u in uploads if u.kind == "document"]
    for doc in documents:
        messages.append(untrusted_context_message(
            f"attached document: {doc.original_name}", doc.text))

    # Images: only a model with MEASURED vision gets them; otherwise the
    # model is told plainly so it can answer honestly instead of hallucinating
    # what it cannot see.
    images = [u for u in uploads if u.kind == "image"]
    vision = bool(runtime.caps and runtime.caps.supports_vision)
    # When images can't be seen, the UI gets an AUTHORITATIVE notice with
    # the diagnosed reason and remedy — not just the model's paraphrase of
    # a prompt note (which read as the model making excuses).
    vision_notice: str | None = None
    if images and vision:
        messages.append({"role": "user",
                         "content": _image_content(images, req.message)})
    else:
        if images and not vision:
            vision_notice = _vision_reason()
            names = ", ".join(u.original_name for u in images)
            messages.append({"role": "user", "content": (
                f"(Note: I attached image(s) — {names} — but this engine "
                f"has no vision projector loaded, so you cannot see them. "
                f"Say so briefly and answer what you can from text.)\n"
                f"{req.message}")})
        else:
            messages.append(history[-1])

    # --- 4. Hand the run to the ONE executor -------------------------------
    # Everything from the first model call to the persisted outcome — the
    # tool loop, scope-filtered catalog, budgets, the stall watchdog, the
    # malformed-call repair, and the run's append-only EVENT LOG — lives
    # in run_executor (stage 4's convergence). The route's job ended at
    # context assembly; the trace tab reads what the executor logged.
    async def event_stream():
        async for frame in stream_chat_run(
            session_id, messages, req.message, is_new, history,
            notice=vision_notice, overrides=req.inference,
        ):
            if frame.get("done"):
                yield "data: [DONE]\n\n"
            else:
                yield f"data: {json.dumps(frame)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        # Anti-buffering headers: without them, proxies and browsers batch
        # the stream into one lump and "streaming" silently isn't.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _live_labels() -> str:
    """The scheduler's in-flight stream labels, joined for cheap substring
    checks (e.g. "chat:4f2a… agent:9c01…"). A session or job is LIVE
    exactly when its id's first 8 chars appear here — labels are minted
    from those ids at admission, so this is the real ledger, not a guess."""
    if not runtime.scheduler:
        return ""
    return " ".join(
        t["label"] for t in runtime.scheduler.snapshot()["active"])


@router.get("/sessions")
async def list_sessions():
    """The sidebar's data: every chat — plain or special — newest activity
    first, each with its KIND (the symbol) and whether it is LIVE in a
    slot right now (the little cycle animation).

    Liveness is derived from the scheduler's real ledger: a stream's
    label carries the first 8 chars of its session or job id, so a chat
    is 'active' exactly when something it started holds a slot.
    """
    live_labels = _live_labels()

    def read() -> list[dict]:
        with SessionLocal() as db:
            # Bounded: the sidebar shows recent conversations, not an
            # unpaged life archive — and an unbounded .all() on the loop
            # thread would stall live streams as the table grows.
            sessions = (db.query(ChatSession)
                        .order_by(ChatSession.updated_at.desc())
                        .limit(200).all())
            return [
                {
                    "id": s.id, "title": s.title, "kind": s.kind or "chat",
                    "active": (s.id[:8] in live_labels
                               or bool(s.job_id) and s.job_id[:8] in live_labels),
                }
                for s in sessions
            ]
    return await asyncio.to_thread(read)


@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    """One conversation's full history (for opening it from the sidebar)."""
    live_labels = _live_labels()

    def read() -> dict:
        """Worker thread: this walks the WHOLE conversation (task
        outcomes make histories big), and on the loop thread it would
        freeze every live token stream — the same rule persist_user_turn
        and list_sessions already follow."""
        with SessionLocal() as db:
            session = db.get(ChatSession, session_id)
            if session is None:
                raise HTTPException(404, "session not found")
            return {
                "id": session.id,
                "title": session.title,
                # Kind + spawned job + liveness: the chat view needs
                # these to re-attach a live progress feed on open.
                "kind": session.kind or "chat",
                "job_id": session.job_id or "",
                "active": (session.id[:8] in live_labels
                           or bool(session.job_id)
                           and session.job_id[:8] in live_labels),
                "messages": [{"role": m.role, "content": m.content}
                             for m in session.messages],
            }
    return await asyncio.to_thread(read)


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """Delete a conversation (its messages cascade with it).

    4.1: the conversation's live work dies WITH it — cancelled and
    AWAITED before the row goes, because after the delete this thread
    would have been the only natural UI for stopping it. A cancellation
    failure blocks the delete and surfaces (never fire-and-forget).
    Completed/paused tasks and saved reports are retained — organizer
    rows tombstone their "Open chat" instead.
    """
    try:
        await research.cancel_session(session_id)
        await discrete.cancel_session(session_id)
    except Exception as error:
        logger.exception("could not stop the conversation's running work")
        raise HTTPException(
            409, f"could not stop this conversation's running work "
                 f"({error}) — nothing was deleted")

    def drop() -> None:
        # Worker thread: the cascade touches every message row.
        with SessionLocal() as db:
            session = db.get(ChatSession, session_id)
            if session is None:
                raise HTTPException(404, "session not found")
            db.delete(session)
            db.commit()
    await asyncio.to_thread(drop)
    return {"deleted": session_id}
