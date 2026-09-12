"""A reply that announces work is not a finished answer."""

from seymour.run_executor import _announces_action

MEASURED = ("A solar system with realistic planets — I'll build it as a single self-contained HTML "
            "file with a canvas renderer. Let me plan it out, then write it in parts.\n\n**Features:**\n"
            "- Canvas rendering, Sun + 8 planets\n- Controls: speed slider, pause/play")


def test_the_measured_plan_prose_counts_as_an_announcement():
    assert _announces_action(MEASURED)


def test_plain_answers_and_replies_with_calls_do_not():
    assert not _announces_action("The sky is blue because sunlight scatters off air molecules.")
    assert not _announces_action("Done. solar.html is written (241 lines) and check_page passed.")
    # A reply that ends in a real call is handled by the call path, not the nudge.
    assert not _announces_action('I will write it now.\n{"tool": "write_file", "args": {"path": "a.html", "content": "x"}}')
    assert not _announces_action("")


def test_repair_guard_blocks_only_failing_measured_pages():
    from seymour.run_executor import _failing_pages
    pages = {
        "a.html": {"verdict": "FIX NEEDED", "report": "console errors: 1 — x is not defined"},
        "b.html": {"verdict": "PASS", "report": "verdict: PASS"},
        "c.html": {"verdict": "not measured", "report": "no browser"},
        "d.py": None,
    }
    failing = _failing_pages(pages)
    assert list(failing) == ["a.html"]          # a not-measured page is never the model's fault


def test_printed_code_without_a_write_is_caught_and_truncation_is_told_apart():
    from seymour.run_executor import _looks_truncated, _printed_code_not_written
    reply = "Here's your clock — 36 lines, one file:\n```html\n<!DOCTYPE html>…\n```"
    assert _printed_code_not_written(reply, "write clock.html: a small html file")
    assert not _printed_code_not_written("Sure, 17*23 is 391.", "what is 17*23")
    assert not _printed_code_not_written('{"tool": "write_file", "args": {"path": "a.html"}}\n```html\n<p>\n```', "write a.html")
    assert _looks_truncated('{"tool": "write_file", "args": {"path": "a.html", "content": "<!DOCTYPE')
    assert not _looks_truncated('{"tool": "write_file", "args": {"path": "a.html", "content": "bad \\\' escape"}}')


def test_a_bare_value_lands_on_a_one_argument_tool():
    from seymour.run_executor import _normalize_args, catalog_for, CHAT_POLICY
    cat = catalog_for(CHAT_POLICY)
    assert _normalize_args(cat, "read_file", {"input": "a.py"}) == {"path": "a.py"}       # one required arg
    assert _normalize_args(cat, "edit_lines", {"input": "a.py"}) == {"input": "a.py"}     # several: left alone
    assert _normalize_args(cat, "read_file", {"path": "b.py"}) == {"path": "b.py"}


def test_a_fence_only_reply_is_the_content_of_a_pending_write_and_failed_writes_are_not_pages():
    from seymour.run_executor import _fence_only, _pending_write_after, _track_page
    body = "import pandas as pd\nprint('hi')"
    assert _fence_only(f"```python\n{body}\n```") == body
    assert _fence_only(f"Here is the file:\n```python\n{body}\n```\nRun it with python clean.py") == body
    assert _fence_only("prose only") is None
    assert _fence_only('{"tool": "write_file", "args": {"path": "a.py"}}\n```python\nx\n```') is None   # a real call: the call path
    assert _fence_only("```a\nx\n```\n```b\ny\n```") is None                                          # two blocks: ambiguous
    pending = _pending_write_after("write_file", {"path": "clean.py", "content": ""},
                                   "Error: write_file got no content — nothing was written.")
    assert pending == {"tool": "write_file", "args": {"path": "clean.py"}}
    assert _pending_write_after("write_file", {"path": "a"}, "[a#1234] written") is None
    class Log: last_check = None
    pages: dict = {}
    _track_page(pages, "write_file", {"path": "clean.py"}, Log(), ok=False)
    assert pages == {}                                                                                 # nothing was written
    _track_page(pages, "write_file", {"path": "clean.py"}, Log(), ok=True)
    assert "clean.py" in pages


async def test_an_empty_round_under_the_cap_is_retried_with_thinking_off(monkeypatch):
    """1,440 hidden tokens and no text (measured 2026-09-12): the run must
    not end 'out of room' — it retries once with thinking off."""
    import uuid
    from seymour import run_executor, runtime
    from seymour.db import ChatSession, SessionLocal, init_db
    from seymour.engine.adapter import GenerationRequest
    from seymour.engine.fake import FakeEngine
    from seymour.scheduler.core import Scheduler
    init_db()

    class Scripted(FakeEngine):
        def __init__(self):
            super().__init__(delay=0.0, tokens=1)
            self.replies = ["", "The answer is 4."]

        async def _generate(self, req: GenerationRequest):
            self.served.append(req)
            reply = self.replies.pop(0) if self.replies else "done"
            if reply:
                yield reply
            req.stats.update({"prompt_tokens": 1, "generated_tokens": 1440 if not reply else 6, "decode_tps": 1.0, "tps_source": "engine"})
    engine = Scripted()
    caps = await engine.capabilities()
    monkeypatch.setattr(runtime, "scheduler", Scheduler(engine, caps))
    monkeypatch.setattr(runtime, "caps", caps)
    monkeypatch.setattr(runtime, "profile", None)
    sid = str(uuid.uuid4())
    with SessionLocal() as db:
        db.add(ChatSession(id=sid, title="t")); db.commit()
    base = [{"role": "system", "content": "sys"}, {"role": "user", "content": "2+2?"}]
    frames = [f async for f in run_executor.stream_chat_run(sid, base, "2+2?", False, base[1:], overrides={"thinking": "on"})]
    text = "".join(f.get("delta", "") for f in frames)
    assert "The answer is 4." in text and "ran out of room" not in text
    assert any(f.get("economy", {}).get("what") == "thinking_off" for f in frames)
    assert engine.served[1].template_kwargs == {"enable_thinking": False}     # the retry closed the channel
