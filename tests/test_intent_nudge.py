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
