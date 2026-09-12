"""The ONE run executor (the harness rebuild's heart, stage 4).

A RUN is one execution spawned by one user message. Its policy is
resolved here, per message, at send time — never latched onto the
conversation — and every single thing the run does appends to its
event log (db.Run / db.RunEvent): each model call, each tool call with
payload and result, each repair, the outcome. The trace UI reads that
log directly; there is no second bookkeeping path to drift from it.

This executor replaces chat's bespoke two-tool loop: a chat run now
carries the AGENT'S tool catalog (scope-filtered), and the model —
not a mode button — decides which tools an input needs. What the old
loop learned the hard way is kept: the 48-char sniff, the trailing-call
detector, the honest empty-reply fallback. What the eval baseline
proved missing is added: hard budgets, a token watchdog (the thinking-
rabbit-hole fix), thinking as an explicit policy knob, and truthful
malformed-call repair for a 35B that misspells tool names routinely.
"""

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass

from seymour import inference, runtime, tools
from seymour.context import Economy, estimate_tokens, strip as strip_meta
from seymour.tools import context as tool_context, todo as todo_tool
from seymour.events import bus
from seymour.db import ChatSession, Message, Run, RunEvent, SessionLocal, utcnow
from seymour.engine.adapter import GenerationRequest
from seymour.guard import untrusted_block
from seymour.memory.extractor import extract_from_messages
from seymour.scheduler.tiers import ModelUnloadingError, Tier

logger = logging.getLogger(__name__)

# How many characters of a reply to buffer before deciding "tool call or
# prose" (long enough to see '{"tool": …', short enough to never delay a
# real answer perceptibly).
SNIFF_CHARS = 48
# Event payloads are BOUNDED at write time: the log must stay cheap to
# read forever. Oversized bodies are excerpted with their true length.
EVENT_EXCERPT = 2000


@dataclass(frozen=True)
class RunPolicy:
    """What this run may do and how hard it may try — resolved per
    message, recorded in the run's start event."""

    preset: str                    # "chat" | "deep_research"
    tool_scope: str                # "read_only" | "full"
    max_tool_calls: int            # hard cap on executed tools
    max_rounds: int                # hard cap on model calls (belt+braces)
    max_tokens: int                # per model call
    enable_thinking: bool          # the Qwen hidden-channel knob
    temperature: float
    first_token_gap_s: float       # watchdog: admission+prefill allowance
    token_gap_s: float             # watchdog: max silence mid-stream


# Chat's resolved policy. Thinking is OFF: the eval baseline traced every
# flaky row to unbounded hidden thinking (180 s of silence on "explain a
# mutex"); a fast, predictable default wins on a local 35B, and thinking
# returns later as an escalation knob, not a default. Scope is FULL —
# a chat can do anything an "agent task" could — but write-tier tools
# pass through the approval gate below: asked once per run, in the
# thread, never a modal.
CHAT_POLICY = RunPolicy(
    preset="chat", tool_scope="full",
    # Sixty tools (was twelve until 2026-09-11). The old cap existed
    # because the harness could not SURVIVE a long run — every result
    # stayed in the prompt forever — so it forbade one, and every real
    # task died on it (the fourth solar-system run "ran out of tool
    # budget" while doing the right thing). The context economy
    # (seymour/context: spill, prune, compact) is what makes a long run
    # affordable; the cap is now a watchdog against a runaway loop, not
    # a budget a healthy task can hit. Rounds are the belt over it (a
    # round without a tool call is a nudge or the answer).
    max_tool_calls=60, max_rounds=90,
    max_tokens=4096, enable_thinking=False, temperature=0.7,
    # Watchdog gaps sized to a WATCHING human, not to infinity. The old
    # 300 s first-token allowance outlived every client that would wait
    # for it: a stall surfaced as the browser's own timeout — a hang
    # with no explanation — instead of Seymour's honest "the model
    # stalled". 120 s still covers a queue wait plus a long prefill.
    first_token_gap_s=120.0, token_gap_s=60.0,
)

# Runs waiting on a human decision: run_id → (event, decision box). A
# write is not a thing to ask forgiveness for, and a modal that steals
# focus is not the answer either — the run pauses IN THE THREAD and the
# person answers there.
_pending: dict[str, tuple[asyncio.Event, dict]] = {}
# How long a run waits for an answer before giving up gracefully. Long
# enough to walk away and come back; short enough that a forgotten
# prompt cannot pin a run forever.
APPROVAL_TIMEOUT_S = 600.0


def decide(run_id: str, allow: bool) -> bool:
    """Answer a run's pending approval (called by the route). Returns
    False when nothing was waiting — a stale click, not an error."""
    waiting = _pending.get(run_id)
    if waiting is None:
        return False
    event, decision = waiting
    decision["allow"] = allow
    event.set()
    return True


# Runs waiting on an ANSWER (ask_user_question): run_id → (event, box).
# Same shape as the approval gate; a different question.
_questions: dict[str, tuple[asyncio.Event, dict]] = {}


def answer(run_id: str, text: str) -> bool:
    """Deliver the person's answer to a run's pending question (the
    route). False when no question was waiting."""
    waiting = _questions.get(run_id)
    if waiting is None:
        return False
    event, box = waiting
    box["answer"] = text
    event.set()
    return True

def catalog_for(policy: RunPolicy) -> dict[str, tools.Tool]:
    """The ONE tool registry, filtered to what this run's scope allows —
    by each tool's own declared tier, not a name list. The same registry
    serves the primary agent; a tool added there is a tool added here."""
    return tools.catalog(policy.tool_scope)


def render_catalog_for(policy: RunPolicy) -> str:
    """The scope-filtered catalog as prompt text — byte-identical per
    policy (the cache rule)."""
    return tools.render_catalog(policy.tool_scope)


# Repeat-call hygiene (dsh's repeat-tool reminder): identical consecutive
# calls get an ADVISORY nudge at these run lengths — appended to the
# result, never replacing it. Small models loop on a failing call; a
# nudge that leaves the decision to the model breaks the loop honestly.
REPEAT_NUDGE_AT = (3, 5, 8)


def _repeat_note(state: dict, name: str, args: dict) -> str:
    """Track consecutive identical calls for one run; return the nudge
    text when a threshold is hit, else an empty string."""
    signature = json.dumps({"tool": name, "args": args}, sort_keys=True, default=str)
    if state.get("last") == signature:
        state["count"] = state.get("count", 1) + 1
    else:
        state["last"], state["count"] = signature, 1
    count = state["count"]
    if count in REPEAT_NUDGE_AT:
        return (f"\n\n[note: you have called {name} {count} times in a row with "
                "identical arguments and received this result each time. Repeating "
                "it will not change it — change the arguments, use a different tool, "
                "or answer with what you have.]")
    return ""


class RunLog:
    """A run's row + its append-only event log, with bounded payloads.

    Synchronous writes on purpose (the chat route's own rule): WAL makes
    each commit ~a millisecond, and log appends must not be interruptible
    by the cancellation that a disconnect brings.
    """

    def __init__(self, session_id: str, policy: RunPolicy) -> None:
        self.id = str(uuid.uuid4())
        self.seq = 0
        with SessionLocal() as db:
            db.add(Run(id=self.id, session_id=session_id,
                       preset=policy.preset))
            db.commit()

    def event(self, type: str, **data) -> None:
        """Append one typed event. Long string values are excerpted with
        their true length noted — the trace stays readable AND honest."""
        bounded = {}
        for key, value in data.items():
            if isinstance(value, str) and len(value) > EVENT_EXCERPT:
                bounded[key] = value[:EVENT_EXCERPT]
                bounded[f"{key}_full_chars"] = len(value)
            else:
                bounded[key] = value
        self.seq += 1
        with SessionLocal() as db:
            db.add(RunEvent(run_id=self.id, seq=self.seq, type=type,
                            data=json.dumps(bounded, ensure_ascii=False)))
            db.commit()

    def finish(self, status: str, **stats) -> None:
        self.event("run_end", status=status, **stats)
        bus.publish("run", "end", run_id=self.id, status=status)
        with SessionLocal() as db:
            run = db.get(Run, self.id)
            if run:
                run.status = status
                run.stats = json.dumps(stats)
                run.finished_at = utcnow()
                db.commit()


class _StallError(Exception):
    """The watchdog fired: the model went silent past the policy's gap."""


async def _watched(stream, policy: RunPolicy):
    """Relay a token stream, raising _StallError on silence. The first
    gap is generous (queue wait + prefill are legitimate silence); after
    tokens flow, a long gap means a stall (with thinking off, healthy
    decode never pauses for minutes)."""
    iterator = stream.__aiter__()
    gap = policy.first_token_gap_s
    while True:
        try:
            token = await asyncio.wait_for(iterator.__anext__(), timeout=gap)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            raise _StallError(f"no tokens for {gap:.0f}s")
        finally:
            pass
        gap = policy.token_gap_s
        yield token


# A reply that ANNOUNCES work instead of doing it. Measured 2026-09-03
# on "write a single html file of a solar system": 2,244 tokens of plan
# ("Let me plan it out, then write it in parts. Features: …") and no
# tool call — the loop took the prose as the finished answer and the run
# ended with nothing written. The nudge below gives such a reply exactly
# one more round with the instruction to act; a reply that merely
# answers (no announcement) is left alone.
_INTENT = re.compile(
    r"\b(let me|i'?ll|i will|i am going to|i'm going to|going to (?:write|build|create|start)|"
    r"then (?:write|build|create)|now (?:write|build|create)|write it in parts|"
    r"here'?s (?:the|my) plan|first,? i)\b", re.IGNORECASE)


_FILE_REQUEST = re.compile(r"\b[\w.-]+\.(?:html?|py|js|css|json|md|txt|csv)\b|\b(?:html|file)\b", re.IGNORECASE)


def _looks_truncated(buffer: str) -> bool:
    """A call the cap cut off never closed its object (or its fence)."""
    tail = buffer.rstrip()
    return not (tail.endswith("}") or tail.endswith("```") or tail.endswith("</tool_call>"))


def _printed_code_not_written(reply: str, request: str) -> bool:
    """The person asked for a file (or an html page) and the reply holds a
    fenced code block but made no tool call: the code went to the chat,
    not to disk — and unwritten code is unchecked code."""
    return (bool(_FILE_REQUEST.search(request or "")) and "```" in reply
            and not _CALL_OPENS.search(reply))


def _announces_action(text: str) -> bool:
    """True when the reply describes work it has not done: an intent phrase
    in the last ~600 characters and no tool call anywhere in it."""
    tail = text.strip()[-600:]
    return bool(tail) and _INTENT.search(tail) is not None and not _CALL_OPENS.search(text)


def _looks_like_tool_call(text: str) -> bool:
    """Our JSON shape, the OpenAI/Hermes shape, or Qwen's <tool_call> tag."""
    return tools.looks_like_call(text, SNIFF_CHARS)


# Where a call might START inside prose (the trailing-call detector below
# looks from here): our object, the Hermes object, or Qwen's tag.
_CALL_OPENS = re.compile(r'<tool_call>|\{\s*"(?:tool|name)"')


async def stream_chat_run(
    session_id: str,
    base_messages: list[dict],
    user_message: str,
    is_new: bool,
    history: list[dict],
    notice: str | None = None,
    overrides: dict | None = None,
) -> "asyncio.AsyncIterator[dict]":
    """Execute one CHAT run, yielding UI frames as it goes.

    `overrides` are this message's inference overrides (the composer's
    quick toggles); they layer on the saved inference settings for this
    run only.

    The route assembles `base_messages` (system prompt + history +
    changing tail — context is ITS job); this function owns everything
    from the first model call to the persisted outcome. Frames keep the
    established SSE contract: session_id / notice / tool / delta /
    replace / stats / error.
    """
    policy = CHAT_POLICY
    catalog = catalog_for(policy)
    # The sampler: saved inference settings + this message's overrides.
    # Chat's measured thinking default is OFF; "auto" keeps that, and the
    # trace records the EFFECTIVE values (a comparison needs them).
    inf = inference.current(overrides)
    thinking_on = inf.enable_thinking(policy.enable_thinking)
    # A model with no hidden channel (the profile measured none) cannot
    # think; the knob is noise there, and "on" would only add a template
    # argument the model ignores. Honest: off, and the trace says why.
    if thinking_on and runtime.profile is not None and not runtime.profile.thinking_default_ok:
        thinking_on = False
        overrides = {**(overrides or {}), "thinking": "off"}
        inf = inference.current(overrides)
    log = RunLog(session_id, policy)
    log.event("run_start", preset=policy.preset,
              tool_scope=policy.tool_scope, thinking=thinking_on,
              max_tool_calls=policy.max_tool_calls, message=user_message,
              inference=inf.trace(thinking_default=policy.enable_thinking))

    visible = ""                       # the reply as the user sees it
    loop = asyncio.get_running_loop()
    started = loop.time()
    first_token_at: float | None = None
    # The context economy for this run: spill / prune / compact, each
    # decision logged as a "context" event (the Runs tab shows it). The
    # convo below carries `_meta` tags; strip_meta() removes them for
    # the engine. The summarizer runs on the same slot as the chat (its
    # cache is about to be rewritten anyway), thinking off, tier 1: the
    # person is waiting on this run.
    async def summarize(messages: list[dict]) -> str:
        return await runtime.scheduler.complete(
            Tier.LIVE_CHAT,
            GenerationRequest(messages=messages, max_tokens=1500, temperature=0.2,
                              cache_key=f"chat:{session_id}",
                              template_kwargs={"enable_thinking": False}),
            label=f"compact:{session_id[:8]}")
    economy = Economy(
        context_tokens=getattr(runtime.caps, "context_per_slot", None),
        reply_tokens=inf.max_tokens, summarize=summarize,
        on_event=log.event, run_id=log.id,
        # The plan (todo_write) rides into every compaction block verbatim.
        extra_state=lambda: todo_tool.render(todo_tool.current(log.id)) if todo_tool.current(log.id) else "")
    convo = economy.tag_base(base_messages)
    # Questions from ask_user_question reach the person through this
    # run's frames; the answer comes back through the /answer route.
    question_frames: list[dict] = []          # drained into the stream by the loop below

    async def ask_person(question: dict) -> str:
        event = asyncio.Event()
        box: dict = {"answer": ""}
        _questions[log.id] = (event, box)
        log.event("note", what="question asked", question=question.get("question"), options=question.get("options"))
        question_frames.append({"question": {"run_id": log.id, **question}})
        try:
            await event.wait()
        finally:
            _questions.pop(log.id, None)
        log.event("note", what="question answered", answer=box["answer"][:500])
        question_frames.append({"answered": True})
        return box["answer"]
    tool_calls = 0                     # executed tools (the real budget)
    rounds = 0                         # model calls (belt + braces)
    writes_allowed = False             # the once-per-run grant (below)
    nudged = False                     # the one-shot "answer now" was sent
    repaired = False                   # the one-shot invalid-call repair
    intent_nudged = False              # the one-shot "you said it, now do it"
    repair_rounds = 0                  # bounded: pages that still fail their check
    pages: dict = {}                   # path → last auto-check (files this run wrote)
    repeat_state: dict = {}            # consecutive identical calls (nudges)
    cache_hits: list[float] = []       # per-round prompt-cache hit ratio (llama.cpp reports it)
    think_retried = False              # the one-shot "budget spent thinking" retry
    request = None
    status = "done"
    try:
        # The run's identity goes first so a consumer can address it (the
        # cancel route, a tab re-attaching through /api/runs/{id}/live).
        yield {"run_id": log.id}
        yield {"session_id": session_id}
        if notice:
            yield {"notice": notice}

        while True:
            rounds += 1
            if rounds > policy.max_rounds:
                # The hard ceiling nothing talks past (dsh has no cap at
                # all and survives on frontier models; a 35B does not).
                log.event("note", what="max_rounds reached")
                break
            # The economy runs before every model call: nothing when the
            # prompt is small, prune under pressure, compact under more.
            # What it did this round is one small frame for the chat (the
            # full account is in the "context" events of the trace).
            tally_before = dict(economy.stats)
            convo = await economy.prepare(convo)
            if economy.stats["compactions"] > tally_before["compactions"]:
                yield {"economy": {"what": "compacted", "messages": economy.stats["compacted_messages"] - tally_before["compacted_messages"],
                                   "tokens": economy.stats["peak_tokens"], "now_tokens": estimate_tokens(convo)}}
            elif economy.stats["prunes"] > tally_before["prunes"]:
                yield {"economy": {"what": "pruned", "results": economy.stats["prunes"] - tally_before["prunes"],
                                   "chars": economy.stats["pruned_chars"] - tally_before["pruned_chars"],
                                   "now_tokens": estimate_tokens(convo)}}
            request = GenerationRequest(
                messages=strip_meta(convo),
                max_tokens=inf.max_tokens,
                cache_key=f"chat:{session_id}",
                **inf.request_kwargs(thinking_default=policy.enable_thinking),
            )
            log.event("model_call", round=rounds,
                      messages=len(convo), max_tokens=inf.max_tokens,
                      thinking=thinking_on, prompt_tokens_est=economy.stats["peak_tokens"])

            buffer = ""
            sniffing = True
            tool_round = False
            round_start = len(visible)
            round_t0 = loop.time()
            progress_at = 0.0          # last tool_progress frame (throttle)
            progress_sent = ""         # decoded content already streamed to the card
            try:
                async for fragment in _watched(
                    runtime.scheduler.stream(
                        Tier.LIVE_CHAT, request,
                        label=f"chat:{session_id[:8]}"),
                    policy,
                ):
                    if first_token_at is None:
                        first_token_at = loop.time()
                    if sniffing:
                        buffer += fragment
                        if len(buffer.lstrip()) >= SNIFF_CHARS:
                            sniffing = False
                            tool_round = _looks_like_tool_call(buffer)
                            if not tool_round:
                                visible += buffer
                                yield {"delta": buffer}
                        continue
                    if tool_round:
                        buffer += fragment
                        # The person sees the call being written: file
                        # name, size so far, the code's tail — never a
                        # bare "…" (measured: 40 s of it on a solar-system
                        # page, then a Stop). Throttled to ~3 frames/s.
                        now = loop.time()
                        if now - progress_at >= 0.35:
                            progress_at = now
                            peek = tools.peek_call(buffer, tail_chars=400_000)
                            full = peek.get("tail") or ""
                            # Send only what is NEW since the last frame; a
                            # decode that no longer extends the previous
                            # text (the fence/field was re-detected) resets.
                            if full.startswith(progress_sent):
                                delta, reset = full[len(progress_sent):], False
                            else:
                                delta, reset = full, True
                            progress_sent = full
                            yield {"tool_progress": {**peek, "tail": full[-400:], "delta": delta,
                                    "reset": reset, "seconds": round(now - round_t0, 1)}}
                            # The Code pane lives outside this stream (a
                            # view switch must not stop the run), so the
                            # same fact rides the bus with a longer tail.
                            bus.publish("run", "tool_progress", run_id=log.id,
                                        session_id=session_id,
                                        **{**tools.peek_call(buffer, tail_chars=8000),
                                           "seconds": round(now - round_t0, 1)})
                    else:
                        visible += fragment
                        yield {"delta": fragment}
            except _StallError as stall:
                # The watchdog: log it, tell the truth, stop the run —
                # a silent 3-minute hang was the baseline's worst bug.
                log.event("note", what="stall", detail=str(stall))
                status = "failed"
                yield {"error": f"the model stalled ({stall}) — try again"}
                break
            log.event("model_result", round=rounds,
                      seconds=round(loop.time() - round_t0, 2),
                      visible_chars=len(visible) - round_start,
                      withheld=tool_round,
                      stats=dict(request.stats) if request.stats else {})
            log.last_raw = buffer if tool_round or sniffing else visible[round_start:]
            # An EMPTY round that spent the whole cap: the budget went into
            # hidden thinking (measured 2026-09-11: 8,192 tokens, 80 s,
            # nothing visible, run ended "ran out of room"). The agent loop
            # learned this on 2026-09-02; the chat run now does the same —
            # retry once with the hidden channel closed, and keep it closed
            # for the rest of this run.
            generated = int((request.stats or {}).get("generated_tokens") or 0)
            if (not buffer.strip() and not visible[round_start:].strip() and thinking_on
                    and generated >= int(inf.max_tokens * 0.9) and not think_retried):
                think_retried = True
                thinking_on = False
                inf = inference.current({**(overrides or {}), "thinking": "off"})
                log.event("note", what="budget spent thinking",
                          detail=f"{generated} tokens, nothing visible — retrying with thinking off for the rest of the run")
                yield {"economy": {"what": "thinking_off", "tokens": generated}}
                continue
            # The cache-hit ledger: every round's ratio, so the run's end
            # can report the mean and the trace can show a miss where it
            # happened (a compaction rewrites the prefix — one miss, expected;
            # a miss on every round is the bug the brief suspects).
            if request.stats.get("cache_hit") is not None:
                cache_hits.append(float(request.stats["cache_hit"]))

            if sniffing:
                tool_round = _looks_like_tool_call(buffer)
                if not tool_round and buffer:
                    visible += buffer
                    yield {"delta": buffer}

            if not tool_round:
                # Prose round — but the model sometimes narrates and
                # drops the JSON at the END (or pretty-prints it). Only
                # a call the reply ENDS with counts: prose after the
                # object means it was QUOTING its protocol.
                tail_call = None
                tail_at = -1
                opens = _CALL_OPENS.search(visible[round_start:])
                if opens and tool_calls < policy.max_tool_calls:
                    tail_at = round_start + opens.start()
                    tail = visible[tail_at:]
                    candidate = tools.parse_call(tail) or {}
                    if str(candidate.get("tool", "")) in catalog:
                        # A fenced file body after the object is PART of
                        # the call (tools.fenced_body), not prose after it.
                        bare = tail
                        if tools.fenced_body(tail) is not None:
                            bare = tail[:tail.rfind("```", 0, tail.rfind("```"))]
                        residue = re.sub(r"\{.*\}", "", bare, flags=re.DOTALL)
                        residue = re.sub(r"```(?:json)?|</?tool_call>", "", residue).strip()
                        if not residue:
                            tail_call = candidate
                if tail_call is None:
                    round_text = visible[round_start:]
                    failing = _failing_pages(pages)
                    if failing and repair_rounds < MAX_REPAIR_ROUNDS and tool_calls < policy.max_tool_calls:
                        # "A final, known working product": the model is
                        # about to finish while a page it wrote still
                        # fails its own check. Bounded repair rounds; the
                        # check's report names the exact problem.
                        repair_rounds += 1
                        log.event("note", what="repair round", round=repair_rounds,
                                  pages={p: c["verdict"] for p, c in failing.items()})
                        convo = convo + [
                            {"role": "assistant", "content": round_text},
                            {"role": "user", "content":
                                "Not finished: the file you wrote still fails its check.\n\n"
                                + "\n\n".join(c["report"] for c in failing.values())
                                + "\n\nFix it now (edit_lines or rewrite the part), then the "
                                  "check runs again automatically. Finish only when it passes."},
                        ]
                        continue
                    if (not intent_nudged and tool_calls < policy.max_tool_calls
                            and not pages and _printed_code_not_written(round_text, user_message)):
                        # It printed the code as prose instead of writing
                        # the file the person asked for: nothing on disk,
                        # nothing checked. One round to write it properly.
                        intent_nudged = True
                        log.event("note", what="printed code, no file written")
                        convo = convo + [
                            {"role": "assistant", "content": round_text},
                            {"role": "user", "content":
                                "You printed the code in the reply but did not write the "
                                "file. Write it now: reply with the write_file call for the "
                                "requested file name WITHOUT a content field, followed by the "
                                "whole file in a ```html … ``` block (fenced form), nothing else."},
                        ]
                        continue
                    if (not intent_nudged and tool_calls < policy.max_tool_calls
                            and _announces_action(round_text)):
                        # It told the person what it would do, then
                        # stopped. One more round, with the order to act
                        # — the prose stays visible, the work follows it.
                        intent_nudged = True
                        log.event("note", what="intent nudge",
                                  detail="the reply announced work without a tool call")
                        convo = convo + [
                            {"role": "assistant", "content": round_text},
                            {"role": "user", "content":
                                "You described what you would do but made no tool call. "
                                "Do it now: reply with ONLY the first tool call (for a "
                                "file, write_file with the full first part), no further "
                                "narration."},
                        ]
                        continue
                    break                  # the visible answer is complete
                round_text = visible[round_start:]
                visible = visible[:tail_at].rstrip() + "\n\n"
                yield {"replace": visible}
                name = str(tail_call.get("tool", ""))
                args = tail_call.get("args") or {}
                tool_calls += 1
                granted = writes_allowed
                async for frame in _gate(log, catalog, name, args, granted):
                    if "granted" in frame:
                        writes_allowed = writes_allowed or frame["granted"]
                        granted = frame["granted"]
                    else:
                        yield frame
                if not _permitted(catalog, name, granted):
                    result = ("Refused: your person declined that change. "
                              "Continue without it, or explain what you "
                              "would have done.")
                    log.event("note", what="write denied", tool=name)
                    convo = convo + [
                        {"role": "assistant", "content": round_text},
                        {"role": "user", "content": result},
                    ]
                    continue
                yield {"tool": {"name": name, "args": args,
                                "summary": _describe(catalog, name, args)}}
                async for frame in _execute_streaming(log, catalog, name, args, economy, ask_person, question_frames):
                    if "result" in frame:
                        result = frame["result"]
                    else:
                        yield frame
                yield _tool_result_frame(name, args, result, log)
                for frame in _side_frames(log, name):
                    yield frame
                _track_page(pages, name, args, log)
                result += _repeat_note(repeat_state, name, args)
                convo = convo + economy.pair(
                    round_text, name, args,
                    _result_content(name, result, log),
                    ok=not tools.is_error(name, result), spill=log.last_spill)
                continue

            # ---- A sniffed whole-JSON tool round -------------------------
            call = tools.parse_call(buffer) or {}
            if not call and not _looks_truncated(buffer):
                # Broken only inside the content string? Salvage the write
                # rather than lose it; the auto-check judges the result.
                salvaged = tools.salvage_call(buffer)
                if salvaged:
                    call = salvaged
                    log.event("note", what="salvaged a write whose JSON did not parse",
                              tool=salvaged["tool"], path=salvaged["args"]["path"],
                              chars=len(salvaged["args"]["content"]))
            name = str(call.get("tool", ""))
            args = call.get("args") or {}
            tool = catalog.get(name)
            if tool is None and not repaired:
                # An invalid call with budget left: name the REAL tools
                # and invite one retry (saying "no more tools" would be
                # a lie the model believes — it would answer a current-
                # events question from stale memory).
                repaired = True
                log.event("repair", got=name or buffer[:120],
                          # The whole failed call (bounded) — a parse failure
                          # is only diagnosable from the text that failed.
                          failed_call=buffer[:8000] if not call else None,
                          available=list(catalog), truncated=not call and _looks_truncated(buffer))
                # A call that LOOKED like one but never parsed was almost
                # always cut off by the reply cap mid-content (measured
                # on ~300-line HTML files). Saying "invalid" makes the
                # model retry the same oversized call; saying "cut off,
                # write it in parts" gives it a move that lands.
                if not call and _looks_truncated(buffer):
                    note = (f"That tool call was cut off by the {inf.max_tokens}-"
                            "token reply cap and was NOT executed. If you were "
                            "writing a long file, write it in parts: write_file "
                            "the first part, then append_file the rest, each part "
                            "well under the cap. Otherwise retry a correct call, or "
                            "answer from what you already know.")
                elif not call:
                    # Complete-looking but unparseable: almost always an
                    # escape inside a code string (\' , a stray quote, a
                    # raw tab). The remedy is the form that has no
                    # escaping at all — measured: a 36-line page failed
                    # this way twice, then the model printed the code as
                    # prose and no file was written.
                    note = ("That tool call was NOT valid JSON and was NOT executed — "
                            "usually a bad escape inside the content string. Send it "
                            "again in the fenced form: the JSON call WITHOUT the content "
                            "field, then the whole file in a ```html … ``` block right "
                            "after it, verbatim, no escaping.")
                else:
                    note = ("That tool call was invalid. Available tools: "
                            + ", ".join(catalog)
                            + ". Retry with a correct call, or answer from "
                              "what you already know.")
                convo = convo + [
                    {"role": "assistant", "content": buffer},
                    {"role": "user", "content": note},
                ]
                continue
            if tool is None or tool_calls >= policy.max_tool_calls:
                # Budget spent (or a second bad call): say so ONCE, then
                # stop generating rather than ping-pong forever.
                if nudged:
                    log.event("note", what="model looped on tool JSON")
                    break
                nudged = True
                convo = convo + [
                    {"role": "assistant", "content": buffer},
                    {"role": "user", "content":
                        "No more tool calls are available. Answer now "
                        "from what you already know."},
                ]
                continue
            tool_calls += 1
            granted = writes_allowed
            async for frame in _gate(log, catalog, name, args, granted):
                if "granted" in frame:
                    writes_allowed = writes_allowed or frame["granted"]
                    granted = frame["granted"]
                else:
                    yield frame
            if not _permitted(catalog, name, granted):
                log.event("note", what="write denied", tool=name)
                convo = convo + [
                    {"role": "assistant", "content": buffer},
                    {"role": "user", "content":
                        "Refused: your person declined that change. "
                        "Continue without it, or explain what you would "
                        "have done."},
                ]
                continue
            yield {"tool": {"name": name, "args": args,
                            "summary": _describe(catalog, name, args)}}
            async for frame in _execute_streaming(log, catalog, name, args, economy, ask_person, question_frames):
                if "result" in frame:
                    result = frame["result"]
                else:
                    yield frame
            yield _tool_result_frame(name, args, result, log)
            for frame in _side_frames(log, name):
                yield frame
            _track_page(pages, name, args, log)
            result += _repeat_note(repeat_state, name, args)
            convo = convo + economy.pair(
                buffer, name, args,
                _result_content(name, result, log),
                ok=not tools.is_error(name, result), spill=log.last_spill)

        # A run that ended with NOTHING visible must say so — a silent
        # empty bubble reads as "broken".
        if not visible and status == "done":
            visible = ("I ran out of room before writing an answer — "
                       "ask again and I'll get straight to it.")
            yield {"delta": visible}

        stats = dict(request.stats) if request and request.stats else {
            "generated_tokens": len(visible.split()),
            "decode_tps": round(
                len(visible.split())
                / max(loop.time() - (first_token_at or started), 0.001), 1),
            "tps_source": "measured",
        }
        stats["elapsed_s"] = round(loop.time() - started, 2)
        if cache_hits:
            stats["cache_hit_mean"] = round(sum(cache_hits) / len(cache_hits), 3)
            stats["cache_hit_rounds"] = len(cache_hits)
        if first_token_at is not None:
            stats["ttft_s"] = round(first_token_at - started, 2)
        if tool_calls:
            stats["tool_rounds"] = tool_calls
        stats["run_id"] = log.id
        yield {"stats": stats}
        yield {"done": True}
    except (asyncio.CancelledError, GeneratorExit):
        # The person pressed Stop (or the tab closed): the SSE consumer
        # went away and this generator is being closed. Measured
        # 2026-09-03: this landed in `finally` with status "done" and
        # nothing to show — a stopped run recorded as a finished one.
        # Say what it was: cancelled, after N seconds, with M characters
        # of whatever call was half-written, so the trace explains the
        # silence the person saw.
        status = "cancelled"
        partial = buffer if (tool_round or sniffing) else ""
        peek = tools.peek_call(partial) if partial else {}
        log.event("note", what="stopped by the person",
                  seconds=round(loop.time() - started, 1),
                  partial_call=peek.get("name"), partial_path=peek.get("path"),
                  partial_chars=len(partial),
                  partial_head=partial[:1500])
        if partial and not visible:
            # Leave a truthful line in the conversation instead of nothing.
            what = (f"a {peek['name']} call" + (f" to {peek['path']}" if peek.get("path") else "")
                    if peek.get("name") else "a reply")
            visible = (f"[stopped after {round(loop.time() - started)} s while generating "
                       f"{what} — {len(partial):,} characters were written and discarded; "
                       f"the Runs tab has the trace]")
        raise
    except ModelUnloadingError as error:
        status = "cancelled"
        log.event("note", what="model unloading", detail=str(error))
        yield {"error": str(error)}
    except Exception as error:
        status = "failed"
        logger.exception("chat run failed")
        # A trace that says "failed" without saying WHY is not a trace.
        # The exception's type, message, and the frame it came from all
        # go in the log — this is the first place anyone will look.
        import traceback
        log.event("error", kind=type(error).__name__, message=str(error),
                  traceback=traceback.format_exc())
        yield {"error": "generation failed"}
    finally:
        # The run's tool-side state goes with it: its plan, its shell
        # session, any question still waiting.
        todo_tool.forget(log.id)
        _questions.pop(log.id, None)
        try:
            from seymour.tools import shell as shell_tools
            await shell_tools.close_all_shells(log.id)
        except Exception:
            pass
        # Persist whatever was generated — a partial reply beats none —
        # and close the run's ledger. Synchronous (the WAL rule).
        if visible:
            with SessionLocal() as db:
                db.add(Message(session_id=session_id, role="assistant",
                               content=visible))
                session = db.get(ChatSession, session_id)
                if session:
                    session.updated_at = utcnow()
                    # The sidebar's LAST-ACTIVITY icon: a chat that used
                    # tools shows the wrench; plain talk, the bubble.
                    session.kind = "agent" if tool_calls else "chat"
                db.commit()
        log.finish(status, tool_calls=tool_calls, rounds=rounds,
                   visible_chars=len(visible),
                   seconds=round(loop.time() - started, 2),
                   # The economy's tally: how many spills, prunes and
                   # compactions the run needed, and its peak prompt size.
                   context=economy.stats,
                   # The measured prompt-cache hit rate across the run's
                   # rounds (None when the engine does not report cache_n).
                   cache_hit_mean=round(sum(cache_hits) / len(cache_hits), 3) if cache_hits else None,
                   cache_hit_min=round(min(cache_hits), 3) if cache_hits else None)
        if visible:
            # Quiet Tier-3 follow-ups: a real title for new
            # conversations, and memory extraction.
            if is_new:
                from seymour.routes.chat import _generate_title
                asyncio.create_task(_generate_title(session_id, user_message))
            asyncio.create_task(extract_from_messages(
                history + [{"role": "assistant", "content": visible}]))


_SYNTHESIZE_NOW = (
    "\n\nUsing this, answer the original question directly and "
    "concisely. SYNTHESIZE: give the one or few most relevant items, "
    "never an inventory of everything in the result, and cite source "
    "URLs inline where useful. If you need one more tool, reply with "
    "ONLY the JSON object and nothing else.")


# The tiers that need the person's say-so: changing files, running code.
_GATED = ("write", "exec")


def _permitted(catalog: dict, name: str, writes_allowed: bool) -> bool:
    """May this call proceed? Read-tier tools always; write- and exec-tier
    only with the run's grant. An unknown name is left to the registry's
    own error text (it teaches the model more than a refusal does)."""
    tool = catalog.get(name)
    if tool is None or tool.tier not in _GATED:
        return True
    return writes_allowed


async def _gate(log: RunLog, catalog: dict, name: str, args: dict,
                writes_allowed: bool):
    """Ask ONCE per run before the first change to the person's files or
    the first command run.

    Yields UI frames; the caller watches for {"granted": bool} to update
    the run's grant. Nick's rule: one yes covers the rest of this run —
    a real checkpoint, not a nag on every write.
    """
    tool = catalog.get(name)
    if tool is None or tool.tier not in _GATED or writes_allowed:
        yield {"granted": writes_allowed}
        return

    run_id = log.id
    event = asyncio.Event()
    decision: dict = {"allow": False}
    _pending[run_id] = (event, decision)
    log.event("note", what="approval requested", tool=name, args=args)
    # The ask lands IN the thread, with the specific action named.
    yield {"approval": {"run_id": run_id, "tool": name, "args": args,
                        "summary": _describe(catalog, name, args)}}
    try:
        await asyncio.wait_for(event.wait(), APPROVAL_TIMEOUT_S)
        allowed = bool(decision["allow"])
    except asyncio.TimeoutError:
        allowed = False                 # unanswered means NO, always
        log.event("note", what="approval timed out", tool=name)
    finally:
        _pending.pop(run_id, None)
    log.event("note", what="approval " + ("granted" if allowed else "denied"),
              tool=name)
    yield {"approved": allowed}
    yield {"granted": allowed}


def _describe(catalog: dict, name: str, args: dict) -> str:
    """One plain line naming what is about to happen — the person is
    approving (and the chat is showing) an ACTION, not a tool name. Each
    tool phrases its own; the registry has a fallback."""
    tool = catalog.get(name)
    if tool is None:
        return f"run {name}"
    if tool.tier == "read":
        # Reads need no approval; this is the chat's live status line.
        if name == "web_search":
            return f"searching the web: {args.get('query', '')}"
        if name == "fetch_page":
            return f"reading {args.get('url', 'a page')}"
        if name == "read_file":
            return f"reading {args.get('path', 'a file')}"
        if name == "grep":
            return f"searching files for {args.get('pattern', '')!r}"
        return "listing files"
    return tools.describe_call(tool, args if isinstance(args, dict) else {})


async def _execute_streaming(log: "RunLog", catalog: dict, name: str, args: dict, economy: Economy,
                             ask_person, question_frames: list[dict]):
    """_execute, but able to yield frames WHILE the tool runs — a question
    card for ask_user_question, its answered marker — then the result as
    {"result": text}. The tool runs as a task; frames queued by the asker
    are relayed as they appear."""
    tokens = tool_context.scope(log.id, 0)
    tool_context.set_asker(ask_person)
    try:
        job = asyncio.create_task(_execute(log, catalog, name, args, economy))
        while not job.done():
            while question_frames:
                yield question_frames.pop(0)
            await asyncio.wait({job}, timeout=0.2)
        while question_frames:
            yield question_frames.pop(0)
        yield {"result": job.result()}
    finally:
        tool_context.set_asker(None)
        tool_context.unscope(tokens)


def _side_frames(log: "RunLog", name: str) -> list[dict]:
    """Frames a tool's run produces for the chat besides its result: the
    plan after todo_write."""
    if name == "todo_write":
        items = todo_tool.current(log.id)
        log.event("todo", items=items)
        return [{"todos": items}]
    return []


def _result_content(name: str, result: str, log: "RunLog"):
    """The result message's content: guard-wrapped text, or — when the
    tool attached an image (read_image) — a content array carrying it."""
    text = untrusted_block(f"{name} result", result) + _SYNTHESIZE_NOW
    parts = getattr(log, "last_attachments", None) or []
    if not parts:
        return text
    return [{"type": "text", "text": text}, *parts]


def _normalize_args(catalog: dict, name: str, args: dict) -> dict:
    """A bare value where an object was expected — {"args": "inv/loader.py"}
    parses to {"input": "inv/loader.py"} — lands on the tool's single
    required argument when it has exactly one; a 35B writes this shape
    for one-argument tools and the retry would only repeat it."""
    tool = catalog.get(name)
    if tool is None or not isinstance(args, dict):
        return args if isinstance(args, dict) else {}
    required = [a for a in tool.args if a not in tool.optional]
    if list(args) == ["input"] and len(required) == 1 and required[0] != "input":
        return {required[0]: args["input"]}
    return args


async def _execute(log: RunLog, catalog: dict, name: str, args: dict,
                   economy: Economy | None = None) -> str:
    """Run one tool through the registry, logging call and result.
    tools.execute never raises — errors come back as readable text, and
    the model reads them and corrects course (that IS the recovery).
    An oversized result is SPILLED by the economy before it enters the
    prompt (the full text goes to an artifact; the model reads an
    excerpt plus the pointer) — never truncated in silence."""
    safe_args = _normalize_args(catalog, name, args if isinstance(args, dict) else {})
    # The raw reply the call was parsed from rides on the event (bounded):
    # when a call arrives with the wrong arguments, the trace must show
    # what the model actually wrote, or the parser cannot be suspected.
    log.event("tool_call", tool=name, args=safe_args, raw=(getattr(log, "last_raw", "") or "")[:600])
    path = str(safe_args.get("path") or "") or None
    bus.publish("run", "tool", run_id=log.id, tool=name, path=path,
                summary=tools.describe_call(catalog[name], safe_args) if name in catalog else name)
    t0 = time.monotonic()
    # For an edit, the chat's diff card wants what CHANGED: the file
    # before and after, diffed here (the tool result only shows the new
    # region). Bounded, best effort, never a reason for the call to fail.
    before_text = _snapshot(path) if name in _DIFFED else None
    # Deliverables are usually MADE BY A COMMAND (a script writes the
    # workbook or the deck), not by a file tool — so the harness watches
    # what a command changed and verifies those files too (measured
    # 2026-09-11: a run wrote orders_clean.xlsx through python thirty
    # times and the xlsx verifier never fired once).
    produced_before = _deliverables_snapshot() if name in _COMMAND_TOOLS else None
    result = await tools.execute(name, safe_args)
    # What the tool attached for the next message (read_image), if anything.
    log.last_attachments = tool_context.take_attachments()
    ok = not tools.is_error(name, result)
    log.last_diff = _diff_of(path, before_text) if before_text is not None and ok else None
    log.last_spill = None
    if economy is not None:
        result, log.last_spill = economy.result_text(name, result)
    # The harness verifies what was written — every time, not when the
    # model remembers to. The report rides on the tool result the model
    # reads next, so a page that throws is fixed in the next round.
    check = None
    if ok and path and name in _FILE_WRITERS:
        from seymour.tools import verify
        try:
            check = await verify.auto_check(path)
        except Exception as error:                  # a checker must never end a run
            logger.warning("auto-check failed for %s: %s", path, error)
        if check:
            result = result.rstrip() + "\n\n" + check["report"]
            log.event("auto_check", path=path, kind=check["kind"], verdict=check["verdict"])
    if produced_before is not None:
        for produced in _deliverables_changed(produced_before)[:MAX_PRODUCED_CHECKS]:
            from seymour.tools import verify
            try:
                check = await verify.auto_check(produced)
            except Exception as error:                  # a checker must never end a run
                logger.warning("auto-check failed for %s: %s", produced, error)
                check = None
            if check:
                result = result.rstrip() + "\n\n" + check["report"]
                log.event("auto_check", path=produced, kind=check["kind"], verdict=check["verdict"], produced_by=name)
                log.last_check = {"path": produced, **check}
                # The card and the repair guard follow the produced file.
                log.produced_paths = getattr(log, "produced_paths", []) + [produced]
    log.last_check = {"path": path, **check} if check and path else getattr(log, "last_check", None)
    log.event("tool_result", tool=name, seconds=round(time.monotonic() - t0, 2),
              is_error=not ok, result=result)
    # The Code pane reads the file back on this; the tag in the result's
    # own [path#TAG] header lets it tell "changed" from "touched".
    tag_match = re.match(r"\[(?P<rel>[^#\]]+)#(?P<tag>[0-9A-F]{4})\]", result or "")
    bus.publish("run", "tool_result", run_id=log.id, tool=name, path=path, ok=ok,
                seconds=round(time.monotonic() - t0, 2),
                tag=tag_match.group("tag") if tag_match else None,
                head=(result or "")[:600], check=log.last_check)
    log.last_tag = tag_match.group("tag") if tag_match else None
    return result


# How many times the loop sends the model back to fix a failing page
# before it lets the run end anyway (and the card shows the red verdict).
MAX_REPAIR_ROUNDS = 2


def _track_page(pages: dict, name: str, args: dict, log: "RunLog") -> None:
    """Remember the last check of every file this run wrote — or that a
    command it ran produced (the repair guard follows both)."""
    path = str((args or {}).get("path") or "")
    if name in _FILE_WRITERS and path:
        pages[path] = getattr(log, "last_check", None) or {"verdict": "not measured", "report": ""}
    for produced in getattr(log, "produced_paths", []) or []:
        check = getattr(log, "last_check", None)
        if check and check.get("path") == produced:
            pages[produced] = check
    log.produced_paths = []


def _failing_pages(pages: dict) -> dict:
    """Files whose last check said FIX NEEDED (not-measured ones are not
    the model's fault and never block the finish). Office files count
    the same as pages: a workbook whose formulas produce #REF! is not
    a finished deliverable."""
    return {p: c for p, c in pages.items() if c and c.get("verdict") == "FIX NEEDED" and c.get("report")}


# Tools whose success means a file on disk changed — the ones auto_check follows.
_FILE_WRITERS = {"write_file", "append_file", "edit_lines", "replace_in_file"}


def _tool_result_frame(name: str, args: dict, result: str, log: "RunLog") -> dict:
    """What the chat's cards need after a tool ran: which file, whether it
    went well, the tag, the check's verdict, the head of the result (a
    terminal card shows the output, a search card the matches) and, for
    an edit, the unified diff with its +/- counts."""
    path = str((args or {}).get("path") or "") or None
    diff = getattr(log, "last_diff", None)
    return {"tool_result": {"name": name, "path": path,
                            "ok": not tools.is_error(name, result),
                            "tag": getattr(log, "last_tag", None),
                            "check": getattr(log, "last_check", None),
                            "head": (result or "")[:2000],
                            "chars": len(result or ""),
                            "spill": (getattr(log, "last_spill", None) or {}).get("path"),
                            "diff": diff}}


# Tools whose result frame carries a before/after diff (the code card
# already shows a whole written file; an edit needs the change itself).
_DIFFED = {"edit_lines", "replace_in_file", "append_file"}
# Tools that run programs — the ones that PRODUCE deliverables indirectly.
_COMMAND_TOOLS = {"run_command", "shell"}
# Files a command may have produced that the verifiers understand.
_DELIVERABLE_SUFFIXES = (".xlsx", ".pptx", ".docx", ".html", ".htm")
MAX_PRODUCED_CHECKS = 3


def _deliverables_snapshot() -> dict[str, float]:
    """mtime of every checkable deliverable in the workspace (bounded walk)."""
    from seymour.tools import paths
    from seymour.tools.files import _walk
    out: dict[str, float] = {}
    try:
        for file in _walk(paths.workspace()):
            if file.suffix.lower() in _DELIVERABLE_SUFFIXES:
                try:
                    out[paths.display(file)] = file.stat().st_mtime
                except OSError:
                    continue
            if len(out) >= 500:
                break
    except Exception:
        pass
    return out


def _deliverables_changed(before: dict[str, float]) -> list[str]:
    """Deliverables that are new or rewritten since the snapshot."""
    after = _deliverables_snapshot()
    return sorted(rel for rel, mtime in after.items() if before.get(rel) != mtime)
# The diff shown in a card is bounded: enough to review, never a 4 MB file.
DIFF_MAX_LINES = 300


def _snapshot(path: str | None) -> str | None:
    """The file's current text, "" when it does not exist yet, None when
    the path is not readable inside the workspace."""
    if not path:
        return None
    from seymour.tools import paths
    try:
        target = paths.resolve(path)
    except ValueError:
        return None
    try:
        return target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
    except OSError:
        return None


def _diff_of(path: str | None, before: str) -> dict | None:
    """A unified diff (2 lines of context) between `before` and the file
    now, with added/removed counts; None when nothing changed."""
    import difflib
    after = _snapshot(path)
    if after is None or after == before:
        return None
    lines = list(difflib.unified_diff(before.splitlines(), after.splitlines(),
                                      fromfile=f"{path} (before)", tofile=f"{path} (after)", lineterm="", n=2))
    added = sum(1 for l in lines[2:] if l.startswith("+"))
    removed = sum(1 for l in lines[2:] if l.startswith("-"))
    cut = len(lines) > DIFF_MAX_LINES
    return {"lines": lines[:DIFF_MAX_LINES], "added": added, "removed": removed, "truncated": cut}
