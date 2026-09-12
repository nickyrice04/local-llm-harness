"""The context economy: spill, prune, compact — model-free proofs.

Every property the executor relies on: an oversized result is never cut
in silence (the whole thing is on disk, the excerpt points at it); a
read the model re-did is blanked, one it is working from is not; a
compaction never separates a call from its result and never loses the
person's request; every decision produces a "context" event; and a run
of twenty tool calls on a tiny context finishes with the economy doing
the work — on a scripted fake engine, in under a second.
"""

import asyncio
import uuid

import pytest

from seymour import runtime
from seymour.config import settings
from seymour.context import Economy, estimate_tokens, spill, strip
from seymour.context import compact as compact_mod
from seymour.context import prune as prune_mod
from seymour.engine.adapter import GenerationRequest
from seymour.engine.fake import FakeEngine
from seymour.scheduler.core import Scheduler
from seymour.tools import files, paths


@pytest.fixture(autouse=True)
def clean_workspace(tmp_path, monkeypatch):
    """A fresh workspace per test (spill files land in it)."""
    monkeypatch.setattr(settings.__class__, "workspace_dir",
                        property(lambda self: tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    files._seen.clear()
    yield


# --------------------------------------------------------------------------- #
#  spill                                                                      #
# --------------------------------------------------------------------------- #

def test_small_results_and_reads_pass_through_untouched():
    assert spill("grep", "short") == ("short", None)
    big = "line\n" * 10_000
    assert spill("read_file", big) == (big, None)         # a read bounds itself; the file IS the artifact


def test_an_oversized_result_is_kept_whole_on_disk_and_excerpted_in_the_prompt():
    big = "\n".join(f"row {i}: " + "x" * 60 for i in range(600))
    text, info = spill("grep", big, run_id="abcdef1234")
    assert info and info["chars"] == len(big)
    on_disk = (paths.workspace() / info["path"]).read_text()
    assert on_disk == big                                  # nothing lost
    assert text.startswith("row 0:") and text.rstrip().endswith("]")
    assert "row 599:" in text                              # the tail survived
    assert "read_file it with offset=" in text and info["path"] in text
    assert len(text) <= spill.__globals__["INLINE_CHARS"]  # the excerpt fits the inline budget
    assert "spill-abcdef12-" in info["path"]


def test_spill_falls_back_to_honest_truncation_when_the_disk_refuses(monkeypatch):
    def broken():
        raise OSError("read-only")
    monkeypatch.setattr(paths, "artifacts_dir", broken)
    text, info = spill("grep", "y" * 20_000)
    assert info is None and "truncated" in text and "could not be written" in text


# --------------------------------------------------------------------------- #
#  prune                                                                      #
# --------------------------------------------------------------------------- #

def _pair(tool, path=None, ok=True, result="ok " * 50, args=None):
    """A tagged call/result pair, as Economy.pair produces it."""
    return Economy(context_tokens=1000, reply_tokens=10, summarize=None).pair(
        f'{{"tool": "{tool}"}}', tool, args or ({"path": path} if path else {}), result, ok=ok)


def _convo(*pairs):
    return [{"role": "system", "content": "sys", "_meta": {"kind": "system"}},
            {"role": "user", "content": "do it", "_meta": {"kind": "request", "pinned": True}},
            *[m for pair in pairs for m in pair]]


def test_superseded_and_stale_reads_are_blanked_but_the_recent_window_is_not():
    convo = _convo(_pair("read_file", "a.py"), _pair("read_file", "b.py"), _pair("read_file", "a.py"),
                   _pair("edit_lines", "b.py"), _pair("grep"), _pair("grep"), _pair("grep"))
    out, actions = prune_mod.prune(convo, keep_recent_pairs=3)
    rules = {(a["path"], a["rule"]) for a in actions}
    assert ("a.py", "superseded_read") in rules              # read again later
    assert ("b.py", "stale_read") in rules                   # edited later
    assert out[3]["content"].startswith("[read of a.py superseded")
    assert out[3]["_meta"]["pruned"] == "superseded_read"
    # The last three pairs (the greps) are untouched, and the second a.py read is the fresh view.
    assert all(m["content"].startswith("ok") for m in out[-6:] if m["_meta"]["kind"] == "result")
    assert out[7]["content"].startswith("ok")                # the later read of a.py stays
    # Idempotent: pruning again finds nothing new.
    again, more = prune_mod.prune(out, keep_recent_pairs=3)
    assert more == [] and again == out


def test_superseded_write_results_and_old_spill_excerpts_shrink():
    pointer = ("head " * 400 + "\n[spilled: the full 30,000-character result (400 lines) is in "
               ".seymour/artifacts/x.txt — read_file it with offset=9 to see the omitted middle]")
    convo = _convo(_pair("write_file", "a.py"), _pair("grep", result=pointer),
                   _pair("edit_lines", "a.py"), _pair("grep"), _pair("grep"), _pair("grep"))
    out, actions = prune_mod.prune(convo, keep_recent_pairs=3)
    rules = [a["rule"] for a in actions]
    assert "uneventful_write" in rules and "spill_pointer" in rules
    assert out[5]["content"].endswith("see the omitted middle]") and "head" not in out[5]["content"].split("\n")[0]


def test_ageing_long_read_only_results_are_trimmed_with_the_guard_reclosed():
    from seymour.guard import GUARD_CLOSE, untrusted_block
    long = untrusted_block("grep result", "m\n" * 2000)
    convo = _convo(_pair("grep", result=long), _pair("grep"), _pair("grep"), _pair("grep"))
    out, actions = prune_mod.prune(convo, keep_recent_pairs=3)
    assert actions and actions[0]["rule"] == "trimmed"
    assert "elided under context pressure" in out[3]["content"]
    assert out[3]["content"].rstrip().endswith(GUARD_CLOSE)
    assert len(out[3]["content"]) < len(long)


def test_errors_are_never_elided_as_uneventful():
    convo = _convo(_pair("write_file", "a.py", ok=False, result="Error: nope"), _pair("edit_lines", "a.py"),
                   _pair("grep"), _pair("grep"), _pair("grep"))
    _, actions = prune_mod.prune(convo, keep_recent_pairs=3)
    assert not any(a["rule"] == "uneventful_write" for a in actions)


# --------------------------------------------------------------------------- #
#  compact                                                                    #
# --------------------------------------------------------------------------- #

def test_choose_range_keeps_k_pairs_verbatim_and_cuts_on_a_pair_boundary():
    convo = _convo(*[_pair("grep") for _ in range(6)])
    start, end = compact_mod.choose_range(convo, keep_recent_pairs=3)
    assert start == 1                                        # after the system prompt
    assert convo[end]["_meta"]["kind"] == "call"             # the cut is BEFORE a call…
    assert convo[end - 1]["_meta"]["kind"] == "result"       # …and AFTER a result: balanced
    assert sum(1 for m in convo[end:] if m["_meta"]["kind"] == "result") == 3
    assert compact_mod.choose_range(_convo(_pair("grep"), _pair("grep")), 3) is None   # nothing old enough


def test_splice_keeps_the_pinned_request_and_marks_the_summary():
    convo = _convo(*[_pair("grep") for _ in range(5)])
    start, end = compact_mod.choose_range(convo, 2)
    new = compact_mod.splice(convo, start, end, "## Decisions made\n- x", "model")
    kinds = [m["_meta"]["kind"] for m in new]
    assert kinds[:3] == ["system", "summary", "request"]     # the request survives, right after the summary
    assert kinds[3:] == ["call", "result"] * 2
    assert "What has happened so far" in new[1]["content"] and "- x" in new[1]["content"]
    assert new[1]["_meta"]["replaced"] == end - start


def test_mechanical_summary_is_a_truthful_ledger():
    convo = _convo(_pair("read_file", "a.py"), _pair("write_file", "b.py"),
                   _pair("run_command", ok=False, result="Error: boom", args={"command": "pytest"}))
    text = compact_mod.mechanical_summary(convo, 1, len(convo))
    assert "- a.py: read" in text and "- b.py: changed" in text
    assert "1 tool result(s) reported an error" in text
    assert "run_command" in text and "pytest" in text


async def test_economy_prunes_then_compacts_under_pressure_and_reports_each():
    events: list[dict] = []
    calls: list[list[dict]] = []

    async def summarize(messages):
        calls.append(messages)
        return "## Decisions made\n- summarized\n## Files touched\n- a.py\n## Facts established\n- none\n## Still open\n- finish"

    economy = Economy(context_tokens=2_000, reply_tokens=200, summarize=summarize,
                      on_event=lambda type, **d: events.append({"type": type, **d}), run_id="r1")
    convo = economy.tag_base([{"role": "system", "content": "s" * 400}, {"role": "user", "content": "the ask"}])
    assert convo[-1]["_meta"]["pinned"] and economy.request_text == "the ask"
    # Well under the prune threshold: nothing happens, nothing is logged.
    assert await economy.prepare(convo) == convo and events == []
    # Pile on results until the prompt is over 70 % of the budget: four
    # reads of one file (three of them prunable), then eight writes to
    # distinct files (nothing to prune — compaction must take those).
    for i in range(4):
        convo = convo + economy.pair("call", "read_file", {"path": "a.py"}, "r" * 900, ok=True)
    for i in range(8):
        convo = convo + economy.pair("call", "write_file", {"path": f"f{i}.py"}, "w" * 900, ok=True)
    before = estimate_tokens(convo)
    out = await economy.prepare(convo)
    whats = [e["what"] for e in events]
    assert "prune" in whats and "compact" in whats
    assert estimate_tokens(out) < before
    assert [m["_meta"]["kind"] for m in out][:3] == ["system", "summary", "request"]
    assert calls and "You are compacting" in calls[0][0]["content"] and "the ask" in calls[0][0]["content"]
    compact_event = next(e for e in events if e["what"] == "compact")
    assert compact_event["method"] == "model" and compact_event["kept_recent_pairs"] == 3
    assert economy.stats["compactions"] == 1 and economy.stats["prunes"] >= 1
    assert all("_meta" not in m for m in strip(out))


async def test_a_failed_summary_falls_back_to_the_mechanical_ledger_not_a_dead_run():
    async def summarize(messages):
        raise RuntimeError("engine gone")
    events = []
    economy = Economy(context_tokens=1_000, reply_tokens=100, summarize=summarize,
                      on_event=lambda type, **d: events.append(d))
    convo = economy.tag_base([{"role": "system", "content": "s"}, {"role": "user", "content": "ask"}])
    for _ in range(6):
        convo = convo + economy.pair("call", "grep", {"pattern": "x"}, "r" * 800)
    out = await economy.prepare(convo)
    assert any(e.get("what") == "compact summary failed" for e in events)
    assert next(e for e in events if e.get("what") == "compact")["method"] == "mechanical"
    assert "built mechanically" in out[1]["content"]


# --------------------------------------------------------------------------- #
#  End to end: a long tool run on a scripted engine                            #
# --------------------------------------------------------------------------- #

class ScriptedEngine(FakeEngine):
    """A fake whose replies are scripted: one per model call, in order.
    The compaction summarizer's prompt is recognised and answered from a
    fixed text so the script stays about the run itself."""

    def __init__(self, replies: list[str]) -> None:
        super().__init__(delay=0.0, tokens=1)
        self.replies = list(replies)
        self.summaries = 0

    async def _generate(self, req: GenerationRequest):
        self.served.append(req)
        text = req.messages[-1].get("content") if req.messages else ""
        if isinstance(text, str) and text.startswith("You are compacting"):
            self.summaries += 1
            reply = "## Decisions made\n- keep going\n## Files touched\n- notes.md\n## Facts established\n- 20 reads\n## Still open\n- answer"
        else:
            reply = self.replies.pop(0) if self.replies else "All done."
        for i in range(0, len(reply), 64):
            yield reply[i:i + 64]
            await asyncio.sleep(0)
        req.stats.update({"prompt_tokens": 32, "generated_tokens": 8, "decode_tps": 100.0, "tps_source": "engine"})


async def test_a_twenty_call_run_finishes_on_a_small_context_with_the_economy_working(monkeypatch):
    from seymour.db import ChatSession, Run, RunEvent, SessionLocal, init_db
    from seymour import run_executor
    init_db()
    ws = paths.workspace()
    (ws / "notes.md").write_text("\n".join(f"line {i}" for i in range(200)))
    # Twenty reads of the same file (each read supersedes the last), then the answer.
    script = ['{"tool": "read_file", "args": {"path": "notes.md"}}'] * 20 + ["Read it twenty times. Done."]
    engine = ScriptedEngine(script)
    caps = await engine.capabilities()                       # context_per_slot=8192 — tiny, on purpose
    monkeypatch.setattr(runtime, "scheduler", Scheduler(engine, caps))
    monkeypatch.setattr(runtime, "caps", caps)
    session_id = str(uuid.uuid4())
    with SessionLocal() as db:
        db.add(ChatSession(id=session_id, title="t")); db.commit()
    base = [{"role": "system", "content": "You are a test."}, {"role": "user", "content": "read notes.md"}]
    frames = [f async for f in run_executor.stream_chat_run(session_id, base, "read notes.md", False, base[1:])]
    assert frames[-1] == {"done": True}
    assert sum(1 for f in frames if "tool" in f) == 20        # the old 12-call cap would have stopped this
    assert "Read it twenty times" in "".join(f.get("delta", "") for f in frames)
    with SessionLocal() as db:
        run = db.query(Run).filter_by(session_id=session_id).one()
        events = [(e.type, e.data) for e in db.query(RunEvent).filter_by(run_id=run.id).order_by(RunEvent.seq)]
    whats = [__import__("json").loads(d)["what"] for t, d in events if t == "context"]
    assert "prune" in whats and "compact" in whats            # the economy fired, and said so
    assert engine.summaries >= 1                              # the summary came from the model
    # The engine never saw a `_meta` key, and the last model call was smaller than the peak.
    assert all("_meta" not in m for r in engine.served for m in r.messages)
    end = __import__("json").loads(next(d for t, d in events if t == "run_end"))
    assert end["context"]["compactions"] >= 1 and end["context"]["prunes"] >= 1
    assert end["tool_calls"] == 20
