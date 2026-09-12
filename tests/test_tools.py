"""Proofs for the tool registry — model-free, fast, and pointed at the
properties the harness depends on: the sandbox boundary holds, edits
refuse stale views, results say when they were cut, commands report
timeouts and exit codes independently, fetched HTML becomes readable
text, and calls parse in every shape a local model produces.
"""

import asyncio
import re

import pytest

from seymour import tools
from seymour.config import settings
from seymour.tools import files, paths, shell, web


@pytest.fixture(autouse=True)
def clean_workspace(tmp_path, monkeypatch):
    """Every test gets a fresh, empty workspace (the real one is Nick's)."""
    monkeypatch.setattr(settings.__class__, "workspace_dir",
                        property(lambda self: tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    files._seen.clear()
    yield


# --------------------------------------------------------------------------- #
#  Registry + parsing                                                         #
# --------------------------------------------------------------------------- #

def test_registry_has_every_family():
    assert {"web_search", "fetch_page", "read_file", "write_file", "edit_lines",
            "replace_in_file", "list_files", "grep", "run_command",
            "remember_fact"} <= set(tools.TOOLS)


def test_read_only_scope_excludes_write_and_exec():
    tiers = {t.tier for t in tools.catalog("read_only").values()}
    assert tiers == {"read"}


def test_parse_call_accepts_every_shape():
    ours = tools.parse_call('{"tool": "grep", "args": {"pattern": "x"}}')
    hermes = tools.parse_call('<tool_call>\n{"name": "grep", "arguments": {"pattern": "x"}}\n</tool_call>')
    fenced = tools.parse_call('```json\n{"tool": "grep", "args": {"pattern": "x"}}\n```')
    stringified = tools.parse_call('{"name": "grep", "arguments": "{\\"pattern\\": \\"x\\"}"}')
    assert ours == hermes == fenced == stringified == {"tool": "grep", "args": {"pattern": "x"}}


def test_parse_call_rejects_nameless_and_prose():
    assert tools.parse_call('{"args": {"pattern": "x"}}') is None
    assert tools.parse_call("just talking about {braces}") is None


def test_looks_like_call_sniffs_both_openers():
    assert tools.looks_like_call('{"tool": "x"')
    assert tools.looks_like_call('<tool_call>{"name"')
    assert not tools.looks_like_call("The answer is {42}")


async def test_unknown_tool_is_a_readable_result():
    result = await tools.execute("calculator", {"x": 1})
    assert result.startswith("Unknown tool: calculator")
    assert "read_file" in result                      # names the real ones
    assert tools.is_error("calculator", result)


# --------------------------------------------------------------------------- #
#  Paths: the sandbox boundary                                                #
# --------------------------------------------------------------------------- #

def test_paths_refuse_escape_and_secrets():
    with pytest.raises(ValueError):
        paths.resolve("../outside.txt")
    with pytest.raises(ValueError):
        paths.resolve("/etc/passwd")
    with pytest.raises(ValueError):
        paths.resolve("keys/id_rsa")
    assert paths.resolve("workspace/notes.md").name == "notes.md"   # prefix stripped
    assert paths.resolve(str(paths.workspace() / "ok.txt")).name == "ok.txt"


# --------------------------------------------------------------------------- #
#  Files: read → edit_lines → chain                                           #
# --------------------------------------------------------------------------- #

async def test_read_numbers_lines_and_tags():
    await files.write_file("a.py", "one\ntwo\nthree\n")
    out = await files.read_file("a.py")
    header = out.split("\n", 1)[0]
    assert re.fullmatch(r"\[a\.py#[0-9A-F]{4}\]", header)
    assert "1:one" in out and "3:three" in out
    assert out.endswith("(End of file — 3 lines.)")


async def test_read_window_footer_names_next_offset():
    await files.write_file("big.txt", "\n".join(f"line {i}" for i in range(1, 501)) + "\n")
    out = await files.read_file("big.txt", limit=10)
    assert "10:line 10" in out and "11:line 11" not in out
    assert "Use offset=11 to continue." in out


async def test_edit_lines_requires_current_tag_and_seen_lines():
    await files.write_file("a.py", "a\nb\nc\nd\n")
    shown = await files.read_file("a.py")
    tag = re.search(r"#([0-9A-F]{4})\]", shown).group(1)
    stale = await files.edit_lines("a.py", "0000", 2, 2, "B")
    assert stale.startswith("Error: stale tag")
    ok = await files.edit_lines("a.py", tag, 2, 3, "B\nC")
    assert ok.startswith(f"[a.py#") and "replaced lines 2-3" in ok
    assert (paths.workspace() / "a.py").read_text() == "a\nB\nC\nd\n"
    # The result carries the NEW tag; chaining an insert with it works
    # without another read.
    new_tag = re.search(r"#([0-9A-F]{4})\]", ok).group(1)
    ins = await files.edit_lines("a.py", new_tag, 1, 0, "zero")
    assert "inserted" in ins
    assert (paths.workspace() / "a.py").read_text() == "zero\na\nB\nC\nd\n"


async def test_edit_lines_reveals_unseen_lines_instead_of_guessing():
    # The file appears on disk WITHOUT the model writing it (a write marks
    # its own lines as seen, rightly — the author knows what it wrote).
    (paths.workspace() / "long.txt").write_text(
        "\n".join(f"L{i}" for i in range(1, 41)) + "\n")
    shown = await files.read_file("long.txt", limit=5)          # saw 1-5 only
    tag = re.search(r"#([0-9A-F]{4})\]", shown).group(1)
    refused = await files.edit_lines("long.txt", tag, 20, 21, "x")
    assert refused.startswith("Error: you are editing lines")
    assert "20:L20" in refused and "21:L21" in refused              # revealed
    assert (paths.workspace() / "long.txt").read_text().count("L20") == 1  # untouched


async def test_edit_lines_noop_is_an_error():
    await files.write_file("a.py", "same\n")
    shown = await files.read_file("a.py")
    tag = re.search(r"#([0-9A-F]{4})\]", shown).group(1)
    out = await files.edit_lines("a.py", tag, 1, 1, "same")
    assert "changes nothing" in out


async def test_replace_in_file_unique_and_hints():
    await files.write_file("r.txt", "alpha\nbeta\nalpha\n")
    dup = await files.replace_in_file("r.txt", "alpha", "gamma")
    assert "occurs 2 times" in dup
    missing = await files.replace_in_file("r.txt", "betta", "x")
    assert "Closest line is 2" in missing
    ok = await files.replace_in_file("r.txt", "beta", "delta")
    assert "replaced 1 occurrence" in ok
    assert (paths.workspace() / "r.txt").read_text() == "alpha\ndelta\nalpha\n"


async def test_write_strips_pasted_numbering():
    await files.write_file("p.txt", "1:hello\n2:world\n")
    assert (paths.workspace() / "p.txt").read_text() == "hello\nworld\n"


async def test_list_and_grep_skip_noise_and_anchor_matches():
    await files.write_file("src/app.py", "def main():\n    return 42\n")
    (paths.workspace() / "node_modules").mkdir()
    (paths.workspace() / "node_modules" / "junk.js").write_text("main")
    listing = await files.list_files()
    assert "src/app.py" in listing and "node_modules" not in listing
    hits = await files.grep("return", glob="*.py")
    assert hits.startswith("[src/app.py#") and "2:    return 42" in hits
    assert "No matches" in await files.grep("nothing_here")


# --------------------------------------------------------------------------- #
#  Shell: confined, bounded, honest                                           #
# --------------------------------------------------------------------------- #

async def test_run_command_exit_code_and_output():
    out = await shell.run_command("echo hi; exit 3")
    assert out.startswith("[exit code 3") and "hi" in out


async def test_run_command_timeout_is_its_own_fact():
    result = await shell.run("sleep 5", timeout_s=1)
    assert result.timed_out and result.exit_code != 0
    assert "TIMED OUT" in shell.render(result)


@pytest.mark.skipif(not shell.SANDBOX_EXEC, reason="sandbox-exec is macOS-only")
async def test_run_command_cannot_write_outside_workspace(tmp_path):
    outside = tmp_path / "escape.txt"
    out = await shell.run_command(f"echo pwned > {outside} && echo wrote")
    assert not outside.exists()
    assert "wrote" not in out.splitlines()[-1] or "Operation not permitted" in out


@pytest.mark.skipif(not shell.SANDBOX_EXEC, reason="sandbox-exec is macOS-only")
async def test_run_command_can_write_inside_workspace():
    out = await shell.run_command("echo ok > made.txt && cat made.txt")
    assert (paths.workspace() / "made.txt").read_text() == "ok\n"
    assert out.startswith("[exit code 0")


async def test_run_command_spills_long_output():
    result = await shell.run("python3 -c \"print('x' * 200000)\"", timeout_s=30)
    assert result.truncated and result.spill_path
    assert (paths.workspace() / result.spill_path).stat().st_size >= 200000
    assert "characters omitted" in result.output


# --------------------------------------------------------------------------- #
#  Web: extraction, focus, search parsing                                     #
# --------------------------------------------------------------------------- #

SAMPLE_HTML = """<html><head><title>A Page</title>
<meta property="og:image" content="https://x.test/img.png"></head>
<body><nav>skip me</nav><script>var x = 1;</script>
<h1>Heading</h1><p>First paragraph about tea.</p>
<ul><li>one</li><li>two</li></ul>
<table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table>
<pre>code here</pre><a href="/next">Next page</a></body></html>"""


def test_extract_html_keeps_structure_drops_chrome():
    page = web.extract_html(SAMPLE_HTML)
    assert page.title == "A Page" and page.image == "https://x.test/img.png"
    assert "# Heading" in page.text and "- one" in page.text
    assert "| a | b |" in page.text and "```" in page.text
    assert "skip me" not in page.text and "var x" not in page.text
    assert ("Next page", "/next") in page.links


def test_focus_excerpts_pick_matching_sections():
    text = "\n\n".join(["Intro about nothing."] * 20 + ["The deploy password is korma-7."]
                       + ["Filler paragraph."] * 20)
    out = web.focus_excerpts(text, "deploy password", budget=600)
    assert "korma-7" in out and out.startswith("[1 of")
    assert "Filler paragraph" not in out


def test_truncate_text_is_honest():
    out = web.truncate_text("word " * 5000, 500)
    assert "truncated at" in out and "document continues" in out


def test_public_ip_filter():
    import ipaddress
    assert web._is_public(ipaddress.ip_address("93.184.216.34"))
    for bad in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.1.1", "::1"):
        assert not web._is_public(ipaddress.ip_address(bad))


async def test_fetch_refuses_private_targets():
    out = await web.fetch_page("http://127.0.0.1:8080/secret")
    assert out.startswith("Refused:")
    out = await web.fetch_page("file:///etc/passwd")
    assert out.startswith("Refused:")


# ---- append_file: long files are written in parts -------------------------
# Why: the first Seymour-vs-dsh comparison (2026-09-02) lost both HTML tasks
# because a ~300-line page could not fit one reply and there was no way to
# continue a file. These pin the part-writer's contract.


async def test_append_file_creates_then_continues_without_gluing_lines():
    assert "created" in await tools.execute("write_file", {"path": "big.html", "content": "<html>\n<body>"})
    out = await tools.execute("append_file", {"path": "big.html", "content": "<p>hi</p>\n</body></html>\n"})
    assert out.startswith("[big.html#") and "appended 2 lines" in out and "now 4 lines" in out
    # The second part started on its own line: "<body>" and "<p>" never merged.
    assert (paths.workspace() / "big.html").read_text() == "<html>\n<body>\n<p>hi</p>\n</body></html>\n"
    # A missing file is created, so "append" is safe as a first part too.
    assert "appended 1 lines" in await tools.execute("append_file", {"path": "new.txt", "content": "x"})


async def test_append_file_refuses_nothing_and_directories():
    assert (await tools.execute("append_file", {"path": "a.txt", "content": ""})).startswith("Error")
    (paths.workspace() / "dir").mkdir()
    assert "is a directory" in await tools.execute("append_file", {"path": "dir", "content": "x"})
    # The catalog names the tool and says what it is for (the model reads this).
    assert "append_file" in tools.render_catalog("full") and "in parts" in tools.render_catalog("full")


def test_agent_step_budget_holds_a_whole_file():
    from seymour.agent import loop
    # 8192 tokens ≈ a think plus a ~300-line file at ~4 chars/token; the
    # deadline must be long enough to actually decode that many tokens.
    assert loop.STEP_MIN_TOKENS >= 8192 and loop.STEP_DEADLINE_S >= 300


# ---- peek_call: reading a half-written call so the UI can show it ---------

def test_peek_call_reads_name_path_and_decoded_tail_from_partial_json():
    partial = ('{"tool": "write_file", "args": {"path": "solar_system.html", '
               '"content": "<!DOCTYPE html>\\n<html lang=\\"en\\">\\n<head>\\n  <title>Solar</title>\\n<script>const x = \\"a')
    view = tools.peek_call(partial)
    assert view["name"] == "write_file" and view["path"] == "solar_system.html"
    assert view["field"] == "content" and view["chars"] == len(partial)
    # Escapes decoded, including the cut-off one at the very end.
    assert view["tail"].startswith("<!DOCTYPE html>\n<html lang=\"en\">")
    assert view["tail"].endswith('const x = "a')


def test_peek_call_before_any_field_and_with_hermes_shape():
    assert tools.peek_call('{"tool": "run_com')["name"] is None      # name not closed yet
    early = tools.peek_call('{"tool": "run_command", "args": {"com')
    assert early["name"] == "run_command" and early["tail"] is None
    hermes = tools.peek_call('{"name": "read_file", "arguments": {"path": "a/b.py"}}')
    assert hermes["name"] == "read_file" and hermes["path"] == "a/b.py"


def test_peek_call_stops_at_the_closing_quote_and_handles_unicode():
    done = tools.peek_call('{"tool": "write_file", "args": {"path": "x.txt", "content": "caf\\u00e9 \\"ok\\""}}')
    assert done["tail"] == 'café "ok"'
    cut = tools.peek_call('{"tool": "write_file", "args": {"path": "x.txt", "content": "abc\\u00e')
    assert cut["tail"] == "abc"                                        # \\u cut short is dropped, no crash


# ---- file content as a fenced block after the call ------------------------

def test_parse_call_attaches_a_trailing_fence_as_content():
    reply = ('{"tool": "write_file", "args": {"path": "page.html"}}\n'
             '```html\n<!DOCTYPE html>\n<p class="x">He said "hi" \\ back</p>\n```\n')
    call = tools.parse_call(reply)
    assert call["tool"] == "write_file" and call["args"]["path"] == "page.html"
    assert call["args"]["content"] == '<!DOCTYPE html>\n<p class="x">He said "hi" \\ back</p>'
    # append_file → content, edit_lines → text; an explicit JSON content wins.
    assert tools.parse_call('{"tool": "append_file", "args": {"path": "a"}}\n```\nmore\n```')["args"]["content"] == "more"
    assert tools.parse_call('{"tool": "edit_lines", "args": {"path": "a", "tag": "AAAA", "start": 1, "end": 1}}\n```py\nx = 1\n```')["args"]["text"] == "x = 1"
    kept = tools.parse_call('{"tool": "write_file", "args": {"path": "a", "content": "json"}}\n```\nfence\n```')
    assert kept["args"]["content"] == "json"


async def test_fenced_write_lands_on_disk_through_execute():
    call = tools.parse_call('{"tool": "write_file", "args": {"path": "f.txt"}}\n```text\nline 1\nline 2\n```')
    out = await tools.execute(call["tool"], call["args"])
    assert "created — 2 lines" in out
    assert (paths.workspace() / "f.txt").read_text() == "line 1\nline 2"


def test_peek_call_shows_a_streaming_fenced_body():
    partial = '{"tool": "write_file", "args": {"path": "p.html"}}\n```html\n<!DOCTYPE html>\n<p>hi'
    view = tools.peek_call(partial)
    assert view["name"] == "write_file" and view["path"] == "p.html"
    assert view["field"] == "content" and view["tail"] == "<!DOCTYPE html>\n<p>hi"


# ---- the parser vs. code inside JSON strings ------------------------------

def test_parse_call_survives_css_braces_bad_escapes_and_raw_newlines_in_content():
    css = '{"tool": "write_file", "args": {"path": "c.html", "content": "<style>body { display: flex; } #x { color: red }</style>"}}'
    call = tools.parse_call(css)
    assert call and call["args"]["content"].startswith("<style>body { display")
    bad_escape = '{"tool": "write_file", "args": {"path": "c.html", "content": "const s = \\\'0\\\';"}}'
    assert tools.parse_call(bad_escape)["args"]["content"] == "const s = '0';"
    raw_newline = '{"tool": "write_file", "args": {"path": "c.html", "content": "line 1\nline 2"}}'
    assert tools.parse_call(raw_newline)["args"]["content"] == "line 1\nline 2"
    # A brace inside a string must not end the object early even when prose follows.
    prose = 'Sure:\n{"tool": "write_file", "args": {"path": "a.js", "content": "if (x) { y() }"}}\nDone.'
    assert tools.parse_call(prose)["args"]["content"] == "if (x) { y() }"


async def test_write_file_refuses_empty_content_instead_of_making_an_empty_file():
    out = await tools.execute("write_file", {"path": "empty.html"})
    assert out.startswith("Error") and "fenced" in out
    assert not (paths.workspace() / "empty.html").exists()


def test_fenced_body_tolerates_trailing_prose_and_a_missing_close():
    trailing = '{"tool": "write_file", "args": {"path": "a.html"}}\n```html\n<p>x</p>\n```\nDone — the page is written.'
    assert tools.parse_call(trailing)["args"]["content"] == "<p>x</p>"
    unclosed = '{"tool": "write_file", "args": {"path": "a.html"}}\n```html\n<p>x</p>\n<p>y</p>'
    assert tools.parse_call(unclosed)["args"]["content"] == "<p>x</p>\n<p>y</p>"


def test_salvage_recovers_a_write_broken_by_a_raw_quote_in_the_content():
    broken = ('{"tool": "write_file", "args": {"path": "clock.html", "content": "<div id="clock">--:--</div>\\n'
              '<script>document.getElementById(\\"clock\\").textContent = \\"x\\";</script>"}}')
    assert tools.parse_call(broken) is None                      # the string boundary is gone
    call = tools.salvage_call(broken)
    assert call and call["salvaged"] and call["args"]["path"] == "clock.html"
    assert call["args"]["content"].startswith('<div id="clock">--:--</div>\n<script>')
    assert 'getElementById("clock")' in call["args"]["content"]
    # Not a file write, or no content shape: nothing salvaged.
    assert tools.salvage_call('{"tool": "run_command", "args": {"command": "ls"}}') is None
    assert tools.salvage_call('{"tool": "write_file", "args": {"path": "a"') is None


def test_parse_call_accepts_the_flat_shape_a_35b_writes():
    """Measured 2026-09-11: {"tool": "read_file", "path": "inv/loader.py"} —
    arguments beside the tool name, not under args. Five of eight calls in
    one run came this way and were answered "path is required"."""
    assert tools.parse_call('{"tool": "read_file", "path": "inv/loader.py"}') == {"tool": "read_file", "args": {"path": "inv/loader.py"}}
    assert tools.parse_call('{"name": "edit_lines", "path": "a.py", "tag": "1F3C", "start": 3, "end": 3, "text": "x"}')["args"] == {
        "path": "a.py", "tag": "1F3C", "start": 3, "end": 3, "text": "x"}
    # The documented shape is untouched, and an explicit empty args stays empty-but-flat-free.
    assert tools.parse_call('{"tool": "list_files", "args": {}}') == {"tool": "list_files", "args": {}}
