"""seymour.tools — everything Seymour can DO besides think.

One registry, used by every run: the chat executor (run_executor.py), the
long-running primary agent and the discrete task pool (agent/loop.py).
"Both should be able to use the same abilities" is a structural fact
here, not a policy — there is exactly one catalog to add a tool to.

Layout (one module per family, each readable on its own):

    paths.py      workspace confinement — the sandbox every file tool obeys
    files.py      read / write / edit / patch / list / grep, line-addressed
    structure.py  glob (the file finder) and read_structure (a file's shape)
    shell.py      run_command (one-shot) and shell (a persistent session),
                  both under macOS sandbox-exec
    jobs.py       run_in_background / job_output / job_kill
    web.py        search providers + guarded fetch + readable extraction
    memory.py     remember_fact
    todo.py       todo_write — the plan, kept by the harness
    ask.py        ask_user_question — one question, in the thread
    images.py     read_image — for a model that can see
    git.py        git_overview / git_file_diff / git_hunk
    subagent.py   task / tasks — child runs with a blank context
    context.py    the run scope tools read (which run, side channels)

Design rules (the tier taxonomy is oh-my-pi's; the guards are dsh's and
Odysseus's — see ACKNOWLEDGMENTS.md):

- Every tool declares an approval tier: "read" (observe), "write"
  (change files in the workspace) or "exec" (run a program in the
  workspace). Chat asks the person once per run before the first write
  or exec; the unattended agent works inside the sandbox without asking
  because the sandbox IS the permission boundary.
- Every result is BOUNDED before it re-enters the prompt, and says so
  when it was cut (dsh: a silent truncation reads as "that was all").
  Long outputs spill in full to an artifact file the model can read.
- Every failure is a tool RESULT, never an exception: the model reads
  the error text and corrects course — that is the recovery mechanism.
- Web content is untrusted: fetches are restricted to public http(s)
  with the TCP connection pinned to the checked address, and the loop
  guard-wraps everything that came from outside.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from seymour.llm_json import parse_json_object

logger = logging.getLogger(__name__)

# The DESIGN budget for a tool result: what a tool aims to return (web
# fetch sizes its extraction to this, read_file its byte window). It is
# the same number as the context economy's inline budget on purpose — a
# result under it enters the prompt whole. Since 2026-09-11 it is no
# longer where results are CUT: execute() bounds at HARD_RESULT_CHARS
# (a belt against a runaway tool) and everything between the two is
# spilled by seymour.context to an artifact the model can read back.
MAX_RESULT_CHARS = 16_000
# The belt: no single result may exceed this even before spilling (a tool
# that returns megabytes is a bug, and the spill file would only hide it).
HARD_RESULT_CHARS = 400_000


@dataclass(frozen=True)
class Tool:
    """One capability the model may invoke."""

    name: str                                     # what the model writes
    description: str                              # shown in the system prompt
    args: dict                                    # arg name → description
    tier: str                                     # "read" | "write" | "exec"
    func: Callable[..., Awaitable[str]]           # the implementation
    # Which args are optional (everything else is required — the catalog
    # marks them so a small model sees the difference).
    optional: frozenset = field(default_factory=frozenset)
    # A one-line human description of an ACTION for the approval card —
    # the person approves "edit notes.md", not "call edit_file".
    describe: Callable[[dict], str] | None = None


def describe_call(tool: Tool, args: dict) -> str:
    """The approval-card line for a call: the tool's own phrasing when it
    has one, a generic one otherwise."""
    if tool.describe is not None:
        try:
            return tool.describe(args)
        except Exception:                         # a describer must never break a run
            pass
    return f"run {tool.name}"


# --------------------------------------------------------------------------- #
#  The registry (assembled from the family modules)                           #
# --------------------------------------------------------------------------- #

from seymour.tools import (  # noqa: E402  (after Tool)
    ask, files, git, images, jobs, memory, probe, shell, skills_tool, structure, subagent, todo, web)

TOOLS: dict[str, Tool] = {
    tool.name: tool
    for tool in [*web.TOOLS, *files.TOOLS, *structure.TOOLS, *shell.TOOLS, *jobs.TOOLS, *memory.TOOLS,
                 *skills_tool.TOOLS, *probe.TOOLS, *todo.TOOLS, *ask.TOOLS, *images.TOOLS, *git.TOOLS,
                 *subagent.TOOLS]
}


def catalog(scope: str = "full") -> dict[str, Tool]:
    """The registry filtered by scope: "full" = everything; "read_only" =
    observing tools only (by each tool's declared tier, not a name list)."""
    if scope == "full":
        return dict(TOOLS)
    return {name: tool for name, tool in TOOLS.items() if tool.tier == "read"}


def render_catalog(scope: str = "full", loop_tools: bool = False) -> str:
    """The tool list as it appears in a system prompt.

    Static text for a given scope — it must be byte-identical on every
    step, or it would break the prompt cache (the byte-identical-prefix
    rule). `loop_tools` appends the two task-mutating tools the agent
    loop implements itself (remember_progress, ask_user) so the agent
    sees ONE complete menu.
    """
    lines = []
    for tool in catalog(scope).values():
        arg_list = ", ".join(
            f'"{a}": <{d}>' + ("" if a not in tool.optional else " (optional)")
            for a, d in tool.args.items())
        lines.append(f"- {tool.name}({arg_list}) — {tool.description}")
    if loop_tools:
        lines.append('- remember_progress("note": <what you learned or did>) — '
                     "update your running notes; do this after every "
                     "significant step.")
        lines.append('- ask_user("question": <what you need decided>) — pause '
                     "and ask your person; REQUIRED before anything "
                     "irreversible or outside your workspace.")
    # The skills INDEX rides under the tools: one line per enabled skill,
    # bodies loaded on demand with use_skill. It is static for a session
    # (the folders are rescanned when Settings changes them), so the
    # prompt's cached prefix survives.
    from seymour import skills
    index = skills.index_text()
    if index:
        lines.append("")
        lines.append("Skills (load one with use_skill before a task it covers):")
        lines.append(index)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  Parsing a call out of model output                                         #
# --------------------------------------------------------------------------- #

# Qwen's native (Hermes) wrapper. The prompt asks for a bare JSON object,
# but a model TRAINED on <tool_call> sometimes reaches for it anyway —
# accepting it costs nothing and saves a repair round.
_TOOL_CALL_TAG = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


# File content as a FENCED BLOCK after the call, instead of a JSON string:
#
#     {"tool": "write_file", "args": {"path": "solar.html"}}
#     ```html
#     <!DOCTYPE html> … the whole file, verbatim, no escaping …
#     ```
#
# Why (measured 2026-09-03): a model spent 330 s generating a 4,760-token
# write_file whose JSON then failed to parse — one unescaped quote or
# backslash anywhere in 14 KB of HTML-inside-a-string breaks the whole
# call. A fence has no escaping to get wrong. The block must FOLLOW the
# object; its body becomes args["content"] (or "text" for edit_lines,
# "new_string" for replace_in_file) when the call did not carry one.
_FENCED_BODY = re.compile(r"\}\s*```[A-Za-z0-9_+-]*[ \t]*\r?\n(.*?)(?:\r?\n?```[^\S\n]*(?:\n.*)?|\Z)\s*$", re.DOTALL)
_BODY_FIELD = {"write_file": "content", "append_file": "content",
               "edit_lines": "text", "replace_in_file": "new_string"}
# The keys that are the CALL's envelope, never an argument (see parse_call).
_ENVELOPE_KEYS = frozenset({"tool", "name", "function", "args", "arguments", "parameters", "id", "type"})


def fenced_body(text: str) -> str | None:
    """The body of a fenced block that closes the reply, or None."""
    match = _FENCED_BODY.search(text)
    return match.group(1) if match else None


def parse_call(text: str) -> dict | None:
    """Find ONE tool call in a reply, in any of the shapes a local model
    produces, normalized to {"tool": name, "args": {...}}:

        {"tool": "x", "args": {...}}            our documented shape
        {"name": "x", "arguments": {...}}       OpenAI / Hermes shape
        <tool_call>{...}</tool_call>            Qwen's native wrapper
        ```json {...} ```                       fenced (llm_json strips it)
        {...call...} then ```lang … ```         file content as a fence

    Returns None when there is no call. Never raises.
    """
    if not text:
        return None
    body = fenced_body(text)
    if body is not None:
        # Parse the object alone; the fence is attached below. The object
        # ends at the last "}" before the fence opener.
        opener = re.search(r"\}\s*```", text)
        text = text[:opener.start() + 1] if opener else text
    match = _TOOL_CALL_TAG.search(text)
    candidate = parse_json_object(match.group(1) if match else text)
    if not isinstance(candidate, dict):
        return None
    name = candidate.get("tool") or candidate.get("name") or candidate.get("function")
    if isinstance(name, dict):                    # {"function": {"name": ..}}
        args = name.get("arguments", {})
        name = name.get("name")
    else:
        args = candidate.get("args", candidate.get("arguments", candidate.get("parameters", {})))
    if not isinstance(name, str) or not name:
        return None
    if isinstance(args, str):                     # stringified arguments
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {"input": args}
    if not isinstance(args, dict):
        args = {}
    if not args:
        # The FLAT shape — {"tool": "read_file", "path": "a.py"} — which a
        # 35B produces routinely (measured 2026-09-11: five of eight calls
        # in one run, each answered "path is required" and retried the same
        # way). Every key that is not the envelope is an argument.
        args = {k: v for k, v in candidate.items() if k not in _ENVELOPE_KEYS}
    field = _BODY_FIELD.get(name.strip())
    if body is not None and field and not args.get(field):
        args = {**args, field: body}
    return {"tool": name.strip(), "args": args}


# A half-written tool call, read without a parser: while the model is
# still generating {"tool": "write_file", "args": {"path": …, "content":
# "<!DOCTYPE html>…, the JSON is not closed and json.loads has nothing to
# say — but the person watching deserves to see the file name and the
# code appearing, not a "…" for three minutes (measured 2026-09-03: a
# solar-system page streamed for 40 s as a blank placeholder and was
# stopped). These regexes pull the name, the path and the growing
# content string out of the raw buffer; _unescape_partial turns the JSON
# escapes back into text even when the string is cut mid-escape.
_PEEK_NAME = re.compile(r'"(?:tool|name)"\s*:\s*"([A-Za-z0-9_.-]+)"')
_PEEK_PATH = re.compile(r'"path"\s*:\s*"((?:[^"\\]|\\.)*)"')
_PEEK_CONTENT = re.compile(r'"(?:content|text|new_string|command|query|url)"\s*:\s*"')
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f"}


def _unescape_partial(raw: str) -> str:
    """Decode JSON string escapes in a string that may end mid-escape."""
    out = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch != "\\":
            out.append(ch); i += 1; continue
        if i + 1 >= len(raw):
            break                                  # a lone trailing backslash
        nxt = raw[i + 1]
        if nxt == "u":
            if i + 6 <= len(raw):
                try:
                    out.append(chr(int(raw[i + 2:i + 6], 16)))
                except ValueError:
                    pass
                i += 6
            else:
                break                              # \uXX cut short
            continue
        out.append(_ESCAPES.get(nxt, nxt)); i += 2
    return "".join(out)


def peek_call(text: str, tail_chars: int = 400) -> dict:
    """What a still-streaming tool call is doing, best effort and never
    raising: {"name", "path", "chars", "tail", "field"}. `tail` is the
    decoded end of the argument being written (content/command/query…),
    or None before that field has started."""
    name = _PEEK_NAME.search(text)
    path = _PEEK_PATH.search(text)
    field = _PEEK_CONTENT.search(text)
    tail = None
    fence = None
    if not field:
        # Content arriving as a fenced block after the object: everything
        # past the fence opener is the file so far (no escapes to decode).
        fence = re.search(r"\}\s*```[A-Za-z0-9_+-]*[ \t]*\r?\n", text)
        if fence:
            tail = text[fence.end():][-tail_chars:]
    if field:
        raw = text[field.end():]
        # The string is closed once an unescaped quote appears: stop there.
        m = re.search(r'(?<!\\)(?:\\\\)*"', raw)
        if m:
            raw = raw[:m.end() - 1]
        decoded = _unescape_partial(raw)
        tail = decoded[-tail_chars:]
    return {
        "name": name.group(1) if name else None,
        "path": _unescape_partial(path.group(1)) if path else None,
        "chars": len(text),
        "tail": tail,
        "field": text[field.start():field.end()].split('"')[1] if field else ("content" if fence else None),
    }


# The last resort for a file write whose JSON will not parse: an
# unescaped quote inside the content ("<div id="clock">" written raw) is
# the one mistake no scanner can undo, because the string boundary is
# gone. But the SHAPE is still obvious — a tool name, a path, and
# everything between `"content": "` and the final `"}}` — and the auto
# check runs on whatever gets written, so a salvage that is wrong is
# caught, while a refusal loses the whole page (measured: 431 tokens,
# twice, on a 36-line clock).
_SALVAGE_CONTENT = re.compile(r'"content"\s*:\s*"(.*)"\s*\}\s*\}\s*$', re.DOTALL)


def salvage_call(text: str) -> dict | None:
    """Recover {"tool", "args": {"path", "content"}} from a write_file /
    append_file call whose JSON is broken only inside the content string.
    None when the shape is not there."""
    name = _PEEK_NAME.search(text)
    path = _PEEK_PATH.search(text)
    if not name or not path or name.group(1) not in ("write_file", "append_file"):
        return None
    body = _SALVAGE_CONTENT.search(text)
    if not body:
        return None
    content = _unescape_partial(body.group(1))
    if not content.strip():
        return None
    return {"tool": name.group(1), "args": {"path": _unescape_partial(path.group(1)), "content": content},
            "salvaged": True}


def looks_like_call(text: str, sniff_chars: int = 48) -> bool:
    """Cheap early test on the first characters of a streaming reply:
    does it start like a call (our JSON, or Qwen's tag)?"""
    stripped = text.lstrip()
    if stripped.startswith("<tool_call>"):
        return True
    head = stripped[:sniff_chars]
    return stripped.startswith("{") and ('"tool"' in head or '"name"' in head)


# --------------------------------------------------------------------------- #
#  Execution                                                                  #
# --------------------------------------------------------------------------- #

def _coerce(tool: Tool, args: dict) -> dict:
    """Map the model's args onto the tool's declared ones. Missing
    optional args are omitted; missing REQUIRED ones become empty
    strings so the tool's own validation names them. Extra keys are
    dropped (a 35B invents them). Values are stringified except for
    booleans and ints, which tools like edit/replace_all/limit need."""
    call_args = {}
    for key in tool.args:
        if key in args and args[key] is not None:
            value = args[key]
            if isinstance(value, (bool, int)):
                call_args[key] = value
            elif isinstance(value, (list, dict)):
                # A structured argument (todo_write's list, tasks' batch)
                # travels as JSON text — str() would give Python repr,
                # which the tool could not parse back.
                call_args[key] = json.dumps(value, ensure_ascii=False)
            else:
                call_args[key] = str(value)
        elif key not in tool.optional:
            call_args[key] = ""
    return call_args


async def execute(name: str, args: dict) -> str:
    """Run one registry tool with validated args; never raise.

    Unknown tools and bad arguments return error TEXT — the result goes
    back into the loop either way, and the model reads the error and
    corrects course (that IS the recovery mechanism).
    """
    tool = TOOLS.get(name)
    if tool is None:
        return (f"Unknown tool: {name}. Available tools: "
                + ", ".join(TOOLS) + ".")
    try:
        result = await tool.func(**_coerce(tool, args if isinstance(args, dict) else {}))
    except Exception as error:
        logger.exception("tool %s failed", name)
        return f"Tool {name} failed: {type(error).__name__}: {error}"
    return bounded(result)


def bounded(text: str, limit: int = HARD_RESULT_CHARS) -> str:
    """Cap a result, SAYING so. Tools that manage their own budget
    (shell, fetch) already come in well under the limit; this is the belt
    against a runaway one. Oversized-but-sane results are not cut here —
    the run's context economy spills them to a file and excerpts them."""
    if len(text) <= limit:
        return text
    return (text[:limit]
            + f"\n[result truncated at {limit} of {len(text)} characters]")


def is_error(name: str, result: str) -> bool:
    """Did this result report a failure? Used by the trace log — the
    prefixes are the ones execute() and the tools themselves emit."""
    return result.startswith(("Unknown tool:", f"Tool {name} failed",
                              "Refused:", "Error:"))
