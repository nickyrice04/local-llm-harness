"""Deep research: a multi-round search-read-synthesize pipeline (Tier 2).

The stage machine (a compact adaptation of Odysseus's DeepResearcher —
see ACKNOWLEDGMENTS.md):

    PLAN      break the question into sub-questions
    round:    QUERIES  → three fresh search queries
              SEARCH   → web search each
              READ     → fetch the most promising new pages, extract what
                         each contributes (guard-wrapped, of course)
              SYNTHESIZE → rewrite the evolving report draft
              DECIDE   → stop, or another round? (min 2, max 4 rounds)
    REPORT    polish the draft; save it to the workspace

Everything model-shaped goes through the scheduler at Tier 2
(FOREGROUND_TASK): the user started it and wants it soon, but is not
watching tokens — so it yields to live chat and outranks the agent.
Partial work is never discarded: on failure or cancellation the evolving
draft is saved as the result (the reference implementation's hard rule).
"""

import asyncio
import contextlib
import json
import logging
import re
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from seymour import runtime
from seymour.agent.tools import fetch_page_with_meta, search_results
from seymour.config import settings
from seymour.engine.adapter import GenerationRequest
from seymour.events import bus
from seymour.guard import escape_markers
from seymour.llm_json import parse_json_array, parse_json_object
from seymour.prompts import load
from seymour.scheduler.tiers import ModelUnloadingError, PreemptedError, Tier

logger = logging.getLogger(__name__)

# ---- Bounded knobs -------------------------------------------------------- #
MIN_ROUNDS = 2            # the decide step can't stop before this
MAX_ROUNDS = 4            # …or continue past this
QUERIES_PER_ROUND = 3     # fresh searches per round
PAGES_PER_ROUND = 3       # new pages read per round
WALL_CLOCK_LIMIT = 15 * 60  # seconds; the whole run's hard ceiling
MAX_EMPTY_ROUNDS = 2      # consecutive useless rounds before assuming the
                          # search path is broken (Odysseus's heuristic —
                          # keep-going would just write a hollow report)

# Extracts matching these are boilerplate, not findings — a cookie banner
# or an empty shell page that the extract model dutifully summarized.
# PHRASES, never bare words (Odysseus's research_utils learned this the
# hard way: bare "cookie"/"copyright" discarded legitimate findings that
# merely discussed cookies or copyright as their subject; see
# ACKNOWLEDGMENTS.md). Applied only to SHORT extracts — see the filter.
_LOW_VALUE_MARKERS = (
    "no relevant information",
    "does not contain",
    "insufficient to",
    "unable to extract",
    "completely unrelated",
    "cookie consent",
    "cookie banner",
    "cookie notice",
    "javascript is required",
    "enable javascript",
    "access denied",
    "all rights reserved",
    "page not found",
    "subscribe to continue",
)


def _source_score(query_terms: set[str], title: str, url: str) -> float:
    """Rank a search result BEFORE spending model time reading it.

    A compact version of Odysseus's ranking heuristic: reward query terms
    in the title (matched on word boundaries, so 'us' never matches
    'business'), reward institutional domains, and penalize link farms.
    Model calls are the scarce resource on a laptop — cheap arithmetic
    that picks better pages multiplies the value of every READ step.
    """
    title_words = set(re.findall(r"[a-z0-9]+", title.lower()))
    # Fraction of the query's terms that appear in the title.
    overlap = len(query_terms & title_words) / max(1, len(query_terms))
    host = urllib.parse.urlparse(url).netloc.lower()
    # Institutional and reference domains earn trust; link farms lose it.
    domain = 0.0
    if host.endswith((".edu", ".gov")) or "wikipedia.org" in host:
        domain = 1.0
    elif host.endswith(".org"):
        domain = 0.5
    penalty = 1.0 if any(
        bad in host for bad in ("pinterest.", "facebook.", "quora.")) else 0.0
    return 2.0 * overlap + domain - penalty


@dataclass
class ResearchJob:
    """One research run's live state (in memory; the report lands on disk)."""

    id: str
    question: str
    session_id: str = ""             # the chat this run was started from
    context: str = ""                # condensed conversation the run inherits
    # running | done | failed | cancelled | declined (declined = the
    # triage step judged there was nothing researchable here and
    # answered as ordinary chat instead — a graceful stop, not a fault).
    status: str = "running"
    phase: str = "planning"          # which stage the UI shows
    round: int = 0
    report: str = ""                 # the evolving draft
    sources: list = field(default_factory=list)   # every useful URL read
    result_path: str = ""            # where the final report was saved
    error: str = ""
    # Run statistics for the report's summary header (the Odysseus
    # format: Duration | Rounds | Queries | URLs — honest numbers only).
    started: float = field(default_factory=time.monotonic)
    queries_run: int = 0             # searches actually executed
    urls_read: int = 0               # pages actually fetched


class ResearchManager:
    """Owns every research job and the asyncio task running each."""

    def __init__(self) -> None:
        self.jobs: dict[str, ResearchJob] = {}
        self._runners: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------ public api
    def start(self, question: str, session_id: str = "",
              context: str = "") -> str:
        """Kick off a run; returns its id immediately (progress via events).

        `context` is an optional condensed transcript from the conversation
        that spun this run off ("yes, and bring context") — it informs the
        PLAN step but never the searches themselves (queries must stay
        focused on the question, not on chat small talk)."""
        job = ResearchJob(id=str(uuid.uuid4()), question=question.strip(),
                          session_id=session_id, context=context.strip())
        self.jobs[job.id] = job
        self._runners[job.id] = asyncio.create_task(
            self._run(job), name=f"research:{job.id[:8]}"
        )
        bus.publish("research", "started", job_id=job.id, question=job.question)
        return job.id

    async def cancel(self, job_id: str) -> None:
        """Stop a run; whatever draft exists is kept as the result."""
        runner = self._runners.get(job_id)
        if runner and not runner.done():
            runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runner

    def get(self, job_id: str) -> Optional[ResearchJob]:
        return self.jobs.get(job_id)

    async def cancel_all(self) -> int:
        """Cancel every running job (cancel-and-unload): drafts are kept
        and each outcome lands in its conversation, as always."""
        count = 0
        for job_id, runner in list(self._runners.items()):
            if not runner.done():
                await self.cancel(job_id)
                count += 1
        return count

    async def cancel_session(self, session_id: str) -> int:
        """Cancel this conversation's running jobs (4.1) — awaited, so
        the caller only deletes once the runners have actually stopped."""
        count = 0
        for job in list(self.jobs.values()):
            if job.session_id == session_id and job.status == "running":
                await self.cancel(job.id)
                count += 1
        return count

    # -------------------------------------------------------------- plumbing
    def _phase(self, job: ResearchJob, phase: str) -> None:
        """Advance the visible phase and tell the UI."""
        job.phase = phase
        bus.publish("research", "phase", job_id=job.id, phase=phase,
                    round=job.round)

    async def _ask(self, job: ResearchJob, prompt: str, max_tokens: int = 2048,
                   temperature: float = 0.3) -> str:
        """One Tier 2 model call, with the preemption contract applied:
        a preempted step waits briefly and retries (bounded)."""
        request = GenerationRequest(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=temperature,
            # LOAD-BEARING: without this, Qwen3.6 "thinks" in its hidden
            # channel and silently burns these tight per-step budgets —
            # the plan/queries steps return truncated nothing, the JSON
            # parse yields [], and the whole run ends "done" with 0
            # sources (the observed bug). Every research step consumes
            # plain visible text, so thinking is off for all of them —
            # the same fix chat's tool rounds already carry.
            template_kwargs={"enable_thinking": False},
        )
        for attempt in range(3):              # bounded retry, never a spin
            try:
                return await runtime.scheduler.complete(
                    Tier.FOREGROUND_TASK, request, label=f"research:{job.id[:8]}"
                )
            except PreemptedError:
                # A live chat needed the slot. Wait a moment; the queue
                # order (tier 2 beats tier 3) gets us back in fairly.
                await asyncio.sleep(1.0 + attempt)
        raise RuntimeError("research step preempted repeatedly")

    # ------------------------------------------------------------- the run
    async def _run(self, job: ResearchJob) -> None:
        """The whole pipeline, top to bottom, with the wall clock watching."""
        started = time.monotonic()
        try:
            # ---- PLAN -----------------------------------------------------
            # ---- TRIAGE: is there anything here to research? --------------
            # Deep research is minutes of work; spending it on a greeting,
            # a vague ask, or something only this person could answer is
            # the failure Nick named. The gate reasons about the MESSAGE
            # (never a keyword list) and, when there's nothing to find
            # out, answers like ordinary chat and stops gracefully.
            self._phase(job, "triage")
            verdict = parse_json_object(await self._ask(job, load(
                "research_triage", question=job.question,
                date=date.today().isoformat(),
            ), max_tokens=512)) or {}
            if verdict.get("researchable") is False:
                job.status = "declined"
                job.report = str(verdict.get("reply", "")).strip() or (
                    "I'd need a specific question to research — what "
                    "would you like me to find out?")
                logger.info("research %s: declined (nothing researchable)",
                            job.id[:8])
                return                    # finally: still saves + delivers

            self._phase(job, "planning")
            plan_reply = await self._ask(job, load(
                "research_plan", question=job.question, date=date.today().isoformat(),
                context=job.context or "(none)",
            ), max_tokens=1024)
            sub_questions = [str(q) for q in parse_json_array(plan_reply)][:5]
            if not sub_questions:
                # A silently empty plan must never neuter the run: fall
                # back to researching the question itself, and say so.
                logger.warning("research %s: plan step returned no "
                               "sub-questions — using the raw question",
                               job.id[:8])
                sub_questions = [job.question]
            seen_urls: set[str] = set()
            all_findings: list[str] = []   # every useful extract, all rounds
            empty_rounds = 0               # consecutive rounds with nothing

            # ---- ROUNDS ---------------------------------------------------
            for job.round in range(1, MAX_ROUNDS + 1):
                # The wall clock outranks everything else.
                if time.monotonic() - started > WALL_CLOCK_LIMIT:
                    break

                # QUERIES: what would most improve the report right now?
                self._phase(job, "searching")
                query_reply = await self._ask(job, load(
                    "research_queries",
                    question=job.question,
                    open_questions="\n".join(f"- {q}" for q in sub_questions),
                    report=job.report[:6000] or "(empty)",
                    n=str(QUERIES_PER_ROUND),
                    date=date.today().isoformat(),
                ), max_tokens=512)
                queries = [str(q) for q in parse_json_array(query_reply)][:QUERIES_PER_ROUND]

                # SEARCH: run the queries; collect fresh scored candidates.
                candidates: dict[str, float] = {}   # url → best score
                for query in queries:
                    job.queries_run += 1
                    # The conversation watches this run live: each real
                    # query is announced as it happens (honest progress,
                    # not a spinner).
                    bus.publish("research", "query", job_id=job.id,
                                query=query, round=job.round)
                    terms = set(re.findall(r"[a-z0-9]+", query.lower()))
                    try:
                        results = await search_results(query)
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        # One failed SEARCH (a DDG 503, a timeout) must
                        # never end the run — the same rule the page
                        # reads below follow. A search path that stays
                        # broken is what the empty-rounds heuristic is
                        # for; it stops the run honestly.
                        logger.info("research %s: search failed for %r (%s)",
                                    job.id[:8], query, error)
                        continue
                    for result in results:
                        url = result["url"]
                        if url in seen_urls:
                            continue
                        # A url found by two queries keeps its BEST score.
                        score = _source_score(terms, result["title"], url)
                        candidates[url] = max(candidates.get(url, -9.0), score)
                # Read the round's budget of pages, best-scored first —
                # model time is the scarce resource; spend it on the pages
                # the cheap heuristic likes most.
                to_read = sorted(candidates, key=candidates.get,
                                 reverse=True)[:PAGES_PER_ROUND]

                # READ: fetch + extract what each page contributes.
                self._phase(job, "reading")
                findings: list[str] = []
                for url in to_read:
                    seen_urls.add(url)
                    job.urls_read += 1
                    bus.publish("research", "read", job_id=job.id, url=url,
                                round=job.round)
                    try:
                        fetched = await fetch_page_with_meta(url)
                        page_text = fetched["text"]
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        # One unreadable page (a 403, a timeout, an SSRF
                        # refusal) must never end the RUN — a real
                        # journal paywall killed a whole run this way.
                        # Skip it and spend the budget on the next page.
                        logger.info("research %s: could not read %s (%s)",
                                    job.id[:8], url, error)
                        bus.publish("research", "read_failed",
                                    job_id=job.id, url=url)
                        continue
                    # fetch_page reports SSRF/redirect refusals as
                    # sentinel strings, not exceptions (its agent-tool
                    # contract) — route them through the same skip path
                    # instead of burning an extract call summarizing a
                    # refusal notice.
                    if page_text.startswith("Refused:"):
                        logger.info("research %s: could not read %s (%s)",
                                    job.id[:8], url, page_text)
                        bus.publish("research", "read_failed",
                                    job_id=job.id, url=url)
                        continue
                    # The extract template wraps {{content}} in guard
                    # markers — so the PAYLOAD must have its own markers
                    # escaped, or a hostile page could embed the literal
                    # closing marker and break out of the untrusted block
                    # (same rule guard.untrusted_block applies). The URL
                    # is untrusted too: one line, escaped.
                    # 1024 tokens: a 200-word summary is ~300 tokens, and
                    # the headroom is cheap insurance against truncation
                    # turning a good extract into unparseable half-JSON
                    # (Odysseus budgets 2048 WITH thinking on; ours is off).
                    extract = await self._ask(job, load(
                        "research_extract",
                        question=job.question,
                        url=escape_markers(url.replace("\n", " "))[:500],
                        content=escape_markers(page_text),
                    ), max_tokens=1024, temperature=0.1)
                    # Two filters before an extract counts as a finding:
                    # the model's own IRRELEVANT verdict, and the
                    # boilerplate markers (a cookie wall summarized
                    # earnestly is still a cookie wall). The markers only
                    # apply to SHORT extracts: a substantive summary that
                    # happens to say "does not contain X, but…" is a
                    # finding, not boilerplate (a measured false-negative:
                    # marker-matching alone rejected real 150-word
                    # extracts and starved runs down to 0 sources).
                    lowered = extract.lower()
                    boilerplate = (len(extract.strip()) < 300
                                   and any(m in lowered
                                           for m in _LOW_VALUE_MARKERS))
                    if "IRRELEVANT" not in extract[:40] and not boilerplate:
                        findings.append(f"[{url}]\n{extract.strip()}")
                        # Sources are RECORDS now: the url plus the
                        # page's og:image, so the report viewer can show
                        # the thumbnail strip (the Odysseus look).
                        job.sources.append(
                            {"url": url, "image": fetched.get("image", "")})
                all_findings.extend(findings)
                bus.publish("research", "round_read", job_id=job.id,
                            round=job.round, pages=len(to_read),
                            useful=len(findings))

                # The broken-search heuristic: rounds that keep producing
                # NOTHING mean the search path is down or the topic is dry.
                # Continuing would only pad time; stop and report honestly.
                empty_rounds = empty_rounds + 1 if not findings else 0
                if empty_rounds >= MAX_EMPTY_ROUNDS:
                    logger.warning("research %s: %d consecutive empty rounds — stopping",
                                   job.id[:8], empty_rounds)
                    break

                # SYNTHESIZE: fold the findings into the evolving draft.
                if findings:
                    self._phase(job, "writing")
                    job.report = (await self._ask(job, load(
                        "research_synthesize",
                        question=job.question,
                        report=job.report or "(empty)",
                        findings="\n\n".join(findings),
                    ), max_tokens=4096)).strip()

                # DECIDE: stop early only after the minimum rounds.
                if job.round >= MIN_ROUNDS:
                    self._phase(job, "deciding")
                    # 16 tokens: YES/NO plus slack for a stray period or
                    # leading space. (Thinking is OFF for this call — with
                    # it on, a reasoning model burns any budget this size
                    # mid-think and the run can never stop early, the trap
                    # Odysseus's 128-token decide still falls into.)
                    verdict = await self._ask(job, load(
                        "research_decide",
                        question=job.question, report=job.report[:8000],
                    ), max_tokens=16, temperature=0.0)
                    if verdict.strip().upper().startswith("YES"):
                        break

            # ---- REPORT ---------------------------------------------------
            self._phase(job, "finalizing")
            if job.report:
                job.report = (await self._ask(job, load(
                    "research_report",
                    question=job.question, report=job.report,
                ), max_tokens=4096)).strip()
            elif all_findings:
                # Synthesis never produced a draft but findings exist —
                # compile them raw rather than reporting nothing (partial
                # work is never discarded; the reference implementation's
                # hard rule, applied one level deeper).
                job.report = ("## Raw findings (synthesis unavailable)\n\n"
                              + "\n\n".join(all_findings))
            # A run that never even SEARCHED is a failure, not a result —
            # reporting it as "done" would dress a broken pipeline up as
            # a dry topic (the observed 0-sources bug's worst symptom).
            if job.queries_run == 0:
                raise RuntimeError(
                    "no search queries were ever generated — the planning "
                    "steps returned nothing usable")
            # Queries ran but NOTHING came back to read: the search path
            # is down (rate-limited, offline, provider changed). Saying
            # "done" here would dress a broken pipeline as a dry topic —
            # the same lie the 0-sources bug told, one layer further in.
            if job.urls_read == 0:
                raise RuntimeError(
                    f"{job.queries_run} searches returned no readable "
                    f"results — the search backend looks unavailable")
            job.status = "done"
        except asyncio.CancelledError:
            job.status = "cancelled"   # the draft below still gets saved
        except ModelUnloadingError:
            # The model was unloaded mid-run: not a failure of the
            # research, and not worth retrying against a draining
            # scheduler. The draft is kept, honestly labeled.
            job.status = "cancelled"
            job.error = "the model was unloaded mid-run"
        except Exception as error:
            logger.exception("research run failed")
            job.status = "failed"
            job.error = str(error)
        finally:
            # Partial work is never discarded: whatever draft exists is
            # written to the workspace, on every exit path.
            self._save(job)
            # The run's outcome lands IN the conversation that started it
            # ("the whole point of the conversation is to see what the
            # model is doing") — deliberately synchronous, like chat's own
            # finally: a WAL commit is ~a millisecond, and a finally that
            # never awaits cannot be interrupted mid-write by the
            # cancellation that brought us here.
            self._persist_chat_message(job)
            bus.publish("research", "finished", job_id=job.id,
                        status=job.status, path=job.result_path)
            # Free the finished runner's Task (jobs stays — list_runs
            # reads it — but a dead asyncio.Task serves nothing).
            self._runners.pop(job.id, None)

    def _persist_chat_message(self, job: ResearchJob) -> None:
        """Write the run's outcome into its originating conversation.

        Done runs get the full report (the deliverable belongs where the
        user asked for it); interrupted runs get the surviving draft;
        empty runs get an honest one-liner naming the error.
        """
        if not job.session_id:
            return                        # started outside any conversation
        if job.status == "declined":
            # Nothing to research: the answer IS the message. No report
            # framing, no apology for a run that never should have run.
            content = job.report
        elif job.status == "done" and job.report:
            content = (
                f"Deep research finished — {len(job.sources)} sources, "
                f"{job.queries_run} searches"
                + (f", saved to {job.result_path}" if job.result_path else "")
                + ".\n\n" + job.report)
        elif job.report:
            content = (f"(research {job.status} — keeping the draft)\n\n"
                       + job.report)
        else:
            content = (f"Deep research {job.status}"
                       + (f": {job.error}" if job.error
                          else " with nothing to report") + ".")
        try:
            from seymour.db import ChatSession, Message, SessionLocal, utcnow
            with SessionLocal() as db:
                db.add(Message(session_id=job.session_id, role="assistant",
                               content=content))
                session = db.get(ChatSession, job.session_id)
                if session:               # outcome = activity (sidebar order)
                    session.updated_at = utcnow()
                db.commit()
        except Exception:
            logger.exception("could not write the research outcome to chat")

    def _save(self, job: ResearchJob) -> None:
        """Write the finished report + its sidecar JSON to the workspace.

        The markdown follows the reference implementation's self-contained
        shape (see ACKNOWLEDGMENTS.md): a Research Summary stats header,
        then the report body (whose prompt asks for inline [text](url)
        citations), then a Sources section — so the FILE alone tells the
        whole story. The sidecar JSON carries the same data structured,
        for anything that wants to render it richer later.
        """
        if job.status == "declined":
            return              # a declined run is a chat reply, not a report
        elapsed = time.monotonic() - job.started
        # A filesystem-safe slug from the question's first words — PLUS
        # the run id: two runs of the same question (or two questions
        # sharing their first 48 chars) must never share a stem, or a
        # cancelled re-run's always-written sidecar would clobber the
        # finished run's archived report.
        slug = re.sub(r"[^a-z0-9]+", "-", job.question.lower())[:48].strip("-")
        stem = f"research-{slug + '-' if slug else ''}{job.id[:8]}"
        path = settings.workspace_dir / f"{stem}.md"
        # The stats header: measured numbers, never adjectives.
        header = (
            f"# Research: {job.question}\n\n"
            f"---\n\n"
            f"## Research Summary\n\n"
            f"**Status:** {job.status} | **Duration:** {elapsed:.0f}s | "
            f"**Rounds:** {job.round} | **Queries:** {job.queries_run} | "
            f"**URLs read:** {job.urls_read} | **Sources kept:** {len(job.sources)}\n\n"
            f"---\n\n"
        )
        # The sources section: every URL that contributed a finding.
        sources = ""
        if job.sources:
            listing = "\n".join(
                f"{i}. {source['url']}"
                for i, source in enumerate(job.sources, start=1))
            sources = f"\n\n---\n\n### Sources\n\n{listing}\n"
        # The markdown only exists when there is a report to put in it —
        # but the SIDECAR always gets written (below): a run that failed
        # empty must still leave its stats on disk, or there is nothing
        # to debug with (the 0-sources bug left no forensics at all).
        if job.report:
            path.write_text(header + job.report + sources, encoding="utf-8")
            job.result_path = str(path)
        # The sidecar JSON: the same run, structured (Odysseus's pattern).
        sidecar = {
            "question": job.question,
            "status": job.status,
            "error": job.error,
            "session_id": job.session_id,
            "report": job.report,
            "sources": job.sources,
            "stats": {
                "duration_s": round(elapsed, 1),
                "rounds": job.round,
                "queries": job.queries_run,
                "urls_read": job.urls_read,
            },
        }
        path.with_suffix(".json").write_text(
            json.dumps(sidecar, indent=2), encoding="utf-8")


# The app-wide instance (constructed here; it holds no resources until used).
research = ResearchManager()
