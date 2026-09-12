"""The workspace file tools: read, write, edit, replace, list, grep.

What a 35B needs from file tools, learned from oh-my-pi's hashline work
and dsh's version-guarded filesystem (see ACKNOWLEDGMENTS.md):

- READ shows every line as `N:text` under a header `[path#TAG]`. The TAG
  is a 4-hex fingerprint of the whole normalized file: any read of the
  same content mints the same tag, and any edit must quote it — so an
  edit anchored on a stale mental model is refused BEFORE it matches
  anything ("re-read the file, then retry"), never applied blind.
- EDIT_LINES replaces an inclusive line range with new text (or inserts,
  or deletes) using the line numbers the model just read. The model
  never has to reproduce old text byte-for-byte — the failure class that
  eats small models alive in old_string/new_string edits. A seen-lines
  guard refuses anchors on lines no read or grep displayed, and the
  refusal REVEALS those lines so one retry fixes it.
- REPLACE_IN_FILE keeps the literal old/new form as a fallback (unique
  match required; the error names the closest line when it isn't found).
- Every edit/write returns the fresh tag and the changed region with its
  NEW numbering, so a chain of edits never needs a re-read in between.
- Output is bounded with omp/dsh's exact-next-action footers ("Use
  offset=N to continue") — no footer means what you see is everything.
"""

import difflib
import fnmatch
import re
import zlib
from pathlib import Path

from seymour.tools import MAX_RESULT_CHARS, Tool, paths

# Read budgets (omp's defaults): lines per call, chars per line, bytes.
READ_DEFAULT_LINES = 300
READ_MAX_LINES = 2000
READ_MAX_LINE_CHARS = 2000
READ_MAX_BYTES = 50 * 1024
# Files larger than this are not text the model should slurp.
MAX_FILE_BYTES = 4 * 1024 * 1024
# How many actual lines a refused edit reveals so the retry can land.
REVEAL_CAP = 40
# Grep/list caps.
GREP_MAX_MATCHES = 100
GREP_MAX_FILES = 20
GREP_LINE_CHARS = 300
LIST_MAX = 200

# The seen-lines ledger: (workspace-relative path, tag) → set of line
# numbers some read/grep DISPLAYED under that tag. Process-local (one
# Seymour, one person): an edit may anchor only on lines the model was
# actually shown, and only while the file still carries that tag.
_seen: dict[tuple[str, str], set[int]] = {}
_SEEN_MAX = 200


def _remember_seen(rel: str, tag: str, lines: range | set) -> None:
    key = (rel, tag)
    if len(_seen) >= _SEEN_MAX and key not in _seen:
        _seen.pop(next(iter(_seen)))                 # oldest out (insertion order)
    _seen.setdefault(key, set()).update(lines)


def compute_tag(text: str) -> str:
    """The content fingerprint: CRC32 of the whitespace-normalized file,
    low 16 bits, 4 uppercase hex (omp uses xxHash32 the same way — the
    point is a short id the model can copy, not cryptography)."""
    normalized = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n"))
    return f"{zlib.crc32(normalized.encode('utf-8')) & 0xFFFF:04X}"


def _read_text(target: Path) -> str:
    """Read a text file or raise ValueError with a model-readable reason."""
    if not target.exists():
        raise ValueError(f"no such file: {paths.display(target)} (use list_files to see what exists)")
    if target.is_dir():
        raise ValueError(f"{paths.display(target)} is a directory — use list_files")
    size = target.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ValueError(f"{paths.display(target)} is {size:,} bytes — too large to read as text")
    data = target.read_bytes()
    if b"\x00" in data[:4096]:
        raise ValueError(f"{paths.display(target)} is binary ({size:,} bytes), not text")
    return data.decode("utf-8", errors="replace")


def _write_text(target: Path, text: str) -> None:
    """Atomic write: temp file in the same directory, then os.replace,
    so a crash mid-write never leaves a half file."""
    import os
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.seymour-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)


def _numbered(lines: list[str], start: int) -> list[str]:
    """`N:text` rows, long lines cut with a marker."""
    out = []
    for offset, line in enumerate(lines):
        if len(line) > READ_MAX_LINE_CHARS:
            line = line[:READ_MAX_LINE_CHARS] + "… (line truncated)"
        out.append(f"{start + offset}:{line}")
    return out


def _to_int(value, default: int) -> int:
    try:
        return int(str(value).strip()) if str(value).strip() else default
    except ValueError:
        return default


# --------------------------------------------------------------------------- #
#  read_file                                                                   #
# --------------------------------------------------------------------------- #

async def read_file(path: str, offset: str | int = "", limit: str | int = "") -> str:
    """Read a window of a file with line numbers and the content tag."""
    try:
        target = paths.resolve(path)
        text = _read_text(target)
    except ValueError as error:
        return f"Error: {error}"
    rel = paths.display(target)
    tag = compute_tag(text)
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()                                  # a trailing newline is not a line
    total = len(lines)
    start = max(1, _to_int(offset, 1))
    count = max(1, min(_to_int(limit, READ_DEFAULT_LINES), READ_MAX_LINES))
    if total == 0:
        _remember_seen(rel, tag, set())
        return f"[{rel}#{tag}] (empty file)"
    if start > total:
        return (f"[{rel}#{tag}] offset {start} is past the end — the file has "
                f"{total} lines. Use offset=1 to read from the top.")
    end = min(total, start + count - 1)
    window = lines[start - 1:end]
    # Byte cap: a file of enormous lines must not blow the prompt.
    body_lines: list[str] = []
    used = 0
    for row in _numbered(window, start):
        if used + len(row) > READ_MAX_BYTES:
            end = start + len(body_lines) - 1
            break
        body_lines.append(row)
        used += len(row) + 1
    _remember_seen(rel, tag, range(start, end + 1))
    if end < total:
        footer = f"(Showing lines {start}-{end} of {total}. Use offset={end + 1} to continue.)"
    elif start > 1:
        footer = f"(Showing lines {start}-{end} — end of file, {total} lines total.)"
    else:
        footer = f"(End of file — {total} lines.)"
    return "\n".join([f"[{rel}#{tag}]", *body_lines, footer])


# --------------------------------------------------------------------------- #
#  write_file                                                                  #
# --------------------------------------------------------------------------- #

_PASTED_PREFIX = re.compile(r"^\d+:", re.MULTILINE)


async def write_file(path: str, content: str) -> str:
    """Create or overwrite a whole file. Returns the fresh tag and, for an
    existing file, the change in lines."""
    try:
        target = paths.resolve(path)
    except ValueError as error:
        return f"Error: {error}"
    if content is None or content == "":
        # Measured 2026-09-03: a fenced-form call whose fence was not
        # attached wrote an EMPTY page, the check passed it, and the run
        # ended "done". An empty write is never what was meant.
        return ("Error: write_file got no content — nothing was written. Put the file "
                "in a fenced block right after the call (```html … ```), or pass "
                "content in the JSON.")
    # A model that pastes what it READ back into a write brings the
    # `N:` prefixes along; strip them when every line carries one.
    body_lines = content.split("\n")
    if content and all(_PASTED_PREFIX.match(line) for line in body_lines if line.strip()):
        content = "\n".join(_PASTED_PREFIX.sub("", line, count=1) for line in body_lines)
    if content.startswith("[") and "#" in content.split("\n", 1)[0] and content.split("\n", 1)[0].endswith("]"):
        content = content.split("\n", 1)[1] if "\n" in content else ""   # a pasted header
    existed = target.exists()
    old = _read_text(target) if existed and not target.is_dir() else ""
    if target.exists() and target.is_dir():
        return f"Error: {paths.display(target)} is a directory"
    _write_text(target, content)
    tag = compute_tag(content)
    rel = paths.display(target)
    new_lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    _remember_seen(rel, tag, range(1, new_lines + 1))
    if not existed:
        return f"[{rel}#{tag}] created — {new_lines} lines, {len(content.encode('utf-8'))} bytes."
    added = removed = 0
    for line in difflib.unified_diff(old.splitlines(), content.splitlines(), lineterm="", n=0):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return (f"[{rel}#{tag}] overwritten — now {new_lines} lines "
            f"(+{added} / -{removed} lines vs before).")


# --------------------------------------------------------------------------- #
#  append_file                                                                 #
# --------------------------------------------------------------------------- #
#
# Why this exists (measured 2026-09-02): asked for a ~300-line HTML page,
# the model's write_file call was cut off by the reply's token cap ELEVEN
# times in a row, and its own plan — "write the first part, then append
# the rest" — had no tool to land on (write_file overwrites; edit_lines
# needs a tag). A file that does not fit one reply must be writable in
# parts, and the simplest honest part-writer is an append.


async def append_file(path: str, content: str) -> str:
    """Append text to the end of a file (creating it if missing). Returns
    the fresh tag and the new line count, so a long file can be written
    in several replies without re-reading it between parts."""
    try:
        target = paths.resolve(path)
    except ValueError as error:
        return f"Error: {error}"
    if target.exists() and target.is_dir():
        return f"Error: {paths.display(target)} is a directory"
    content = content if content is not None else ""
    if not content:
        return "Error: nothing to append (content is empty)."
    old = _read_text(target) if target.exists() else ""
    # Never glue a new part onto the tail of the previous one mid-line:
    # if the file does not end with a newline, start the part on a new one.
    joiner = "" if not old or old.endswith("\n") else "\n"
    text = old + joiner + content
    _write_text(target, text)
    tag = compute_tag(text)
    rel = paths.display(target)
    total = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    added = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    _remember_seen(rel, tag, range(1, total + 1))
    # Parts cannot see each other: measured 2026-09-02, a page written in
    # three parts wired a button to a function that no part defined, and
    # every static check still passed. The result says what to do once
    # the last part lands, in the tool's own voice.
    return (f"[{rel}#{tag}] appended {added} lines — now {total} lines, "
            f"{len(text.encode('utf-8'))} bytes. When the file is complete, "
            "read_file it once and check that every function, id and variable "
            "one part uses is defined in another — parts cannot see each other.")


# --------------------------------------------------------------------------- #
#  edit_lines                                                                  #
# --------------------------------------------------------------------------- #

def _reveal(lines: list[str], start: int, end: int) -> str:
    """Up to REVEAL_CAP actual lines around a refused range, numbered, so
    the model can re-anchor without another read."""
    lo = max(1, start - 2)
    hi = min(len(lines), max(end, start) + 2)
    if hi - lo + 1 > REVEAL_CAP:
        hi = lo + REVEAL_CAP - 1
    return "\n".join(_numbered(lines[lo - 1:hi], lo))


async def edit_lines(path: str, tag: str, start: str | int, end: str | int,
                     text: str = "") -> str:
    """Replace lines start..end (inclusive, as numbered in your latest
    read) with `text`. end = start-1 inserts before `start`; empty text
    deletes the range. The tag must match the file's current tag."""
    try:
        target = paths.resolve(path)
        current = _read_text(target)
    except ValueError as error:
        return f"Error: {error}"
    rel = paths.display(target)
    live_tag = compute_tag(current)
    wanted = (tag or "").strip().upper().lstrip("#")
    if wanted != live_tag:
        return (f"Error: stale tag {wanted or '(none)'} — {rel} is now #{live_tag}. "
                "The file changed since you read it (or you never read it); "
                "read_file it, then retry with the current tag and line numbers.")
    lines = current.split("\n")
    trailing_newline = lines and lines[-1] == ""
    if trailing_newline:
        lines.pop()
    total = len(lines)
    s = _to_int(start, 0)
    e = _to_int(end, s)
    if s < 1 or s > total + 1 or e < s - 1 or e > total:
        return (f"Error: line range {s}-{e} is out of bounds — {rel} has {total} lines. "
                "Use the numbers from your latest read (end may be start-1 to insert).")
    # The seen-lines guard: every line the edit touches (or, for an
    # insert, the anchor line) must have been displayed under this tag.
    seen = _seen.get((rel, live_tag), set())
    touched = set(range(s, e + 1)) if e >= s else {min(s, total)} - {0}
    if touched and not touched <= seen:
        return (f"Error: you are editing lines of {rel} that no read showed you "
                f"under tag #{live_tag}. Here they are — check them, then retry:\n"
                + _reveal(lines, s, e))
    new_lines = text.split("\n") if text else []
    if text.endswith("\n"):
        new_lines.pop()                              # a trailing newline is not an extra line
    # A model that echoes its numbering into the replacement text.
    if new_lines and all(_PASTED_PREFIX.match(line) for line in new_lines if line.strip()):
        new_lines = [_PASTED_PREFIX.sub("", line, count=1) for line in new_lines]
    old_slice = lines[s - 1:e] if e >= s else []
    if old_slice == new_lines:
        return (f"Error: that edit changes nothing — lines {s}-{e} of {rel} already "
                "read exactly that. Re-read the region if this is unexpected.")
    updated = lines[:s - 1] + new_lines + lines[e:]
    result = "\n".join(updated) + ("\n" if trailing_newline or not updated else "")
    _write_text(target, result)
    new_tag = compute_tag(result)
    # Show the changed region with its NEW numbering (plus a line of
    # context each side) and record it as seen — the next edit can chain.
    lo = max(1, s - 1)
    hi = min(len(updated), s + len(new_lines))
    region = _numbered(updated[lo - 1:hi], lo) if updated else []
    _remember_seen(rel, new_tag, range(1, len(updated) + 1))
    verb = ("inserted" if e < s else "deleted" if not new_lines else "replaced")
    return "\n".join([
        f"[{rel}#{new_tag}] {verb} lines {s}-{e} → {len(new_lines)} line(s); "
        f"file is now {len(updated)} lines.",
        *region,
        "(Use the new tag and numbers for further edits.)",
    ])


# --------------------------------------------------------------------------- #
#  replace_in_file                                                             #
# --------------------------------------------------------------------------- #

async def replace_in_file(path: str, old_string: str, new_string: str,
                          replace_all: str | bool = False) -> str:
    """Literal replacement: old_string must occur exactly once (or pass
    replace_all). The fallback for edits where you know the exact text."""
    try:
        target = paths.resolve(path)
        current = _read_text(target)
    except ValueError as error:
        return f"Error: {error}"
    rel = paths.display(target)
    if not old_string:
        return "Error: old_string is required (use write_file to create a file)"
    if old_string == new_string:
        return "Error: old_string and new_string are identical"
    everywhere = str(replace_all).strip().lower() in ("true", "1", "yes")
    count = current.count(old_string)
    if count == 0:
        # Point at the closest line so the retry can quote it exactly.
        probe = old_string.strip().split("\n")[0][:120]
        close = difflib.get_close_matches(probe, [l.strip() for l in current.split("\n")], n=1, cutoff=0.6)
        hint = ""
        if close and close[0]:
            index = next((i for i, l in enumerate(current.split("\n"), 1) if l.strip() == close[0]), None)
            if index:
                hint = f" Closest line is {index}: {close[0][:160]!r}."
        return (f"Error: old_string not found in {rel}.{hint} Read the file and "
                "match it exactly, or use edit_lines with line numbers.")
    if count > 1 and not everywhere:
        first = [i for i, l in enumerate(current.split("\n"), 1) if old_string.split("\n")[0] in l][:3]
        return (f"Error: old_string occurs {count} times in {rel} (lines {first}…). "
                "Add surrounding lines to make it unique, or set replace_all=true.")
    updated = current.replace(old_string, new_string) if everywhere else current.replace(old_string, new_string, 1)
    _write_text(target, updated)
    new_tag = compute_tag(updated)
    _remember_seen(rel, new_tag, range(1, updated.count("\n") + 2))
    # Where did it land? The first changed line, with a little context.
    changed_at = next((i for i, (a, b) in enumerate(
        zip(current.split("\n"), updated.split("\n")), 1) if a != b), 1)
    new_lines = updated.split("\n")
    lo, hi = max(1, changed_at - 1), min(len(new_lines), changed_at + max(1, new_string.count("\n") + 1))
    return "\n".join([
        f"[{rel}#{new_tag}] replaced {count if everywhere else 1} occurrence(s).",
        *_numbered(new_lines[lo - 1:hi], lo),
    ])


# --------------------------------------------------------------------------- #
#  list_files / grep                                                           #
# --------------------------------------------------------------------------- #

def _walk(root: Path):
    """Every file under root, skipping noise dirs, in sorted order."""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for entry in reversed(entries):                  # reversed: stack pops in order
            if entry.is_symlink():
                continue                                 # never follow links out
            if entry.is_dir():
                if entry.name not in paths.SKIP_DIRS:
                    stack.append(entry)
            else:
                yield entry


async def list_files(path: str = "", pattern: str = "") -> str:
    """Files under a folder (default: the whole workspace), optionally
    filtered by a glob on the workspace-relative path."""
    try:
        root = paths.resolve(path, allow_root=True)
    except ValueError as error:
        return f"Error: {error}"
    if not root.exists():
        return f"Error: no such folder: {paths.display(root)}"
    if root.is_file():
        return f"{paths.display(root)}  ({root.stat().st_size:,} bytes)"
    rows = []
    total = 0
    for file in _walk(root):
        rel = paths.display(file)
        if pattern and not (fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(file.name, pattern)):
            continue
        total += 1
        if len(rows) < LIST_MAX:
            try:
                rows.append((rel, file.stat().st_size))
            except OSError:
                continue
    # Token economy: sizes are worth their tokens on a short listing and
    # noise on a long one (a 35B fed 90 sized rows spent its whole step
    # budget thinking about how to repeat them). Past 40 files, names only.
    rows = [f"{rel}  ({size:,} bytes)" if len(rows) <= 40 else rel
            for rel, size in rows]
    where = paths.display(root)
    if not rows:
        return (f"No files{' matching ' + repr(pattern) if pattern else ''} under "
                f"{where if where != '.' else 'the workspace'} (it is empty — "
                "write_file creates files).")
    header = f"{total} file(s) under {where if where != '.' else 'the workspace'}"
    if total > LIST_MAX:
        header += f" — showing {LIST_MAX}; narrow with pattern"
    return "\n".join([header + ":", *rows])


async def grep(pattern: str, path: str = "", glob: str = "",
               ignore_case: str | bool = False) -> str:
    """Regex search across workspace files. Matches print as `N:text`
    under a `[path#TAG]` header, so edit_lines can anchor on them."""
    if not (pattern or "").strip():
        return "Error: grep needs a pattern"
    flags = re.IGNORECASE if str(ignore_case).strip().lower() in ("true", "1", "yes") else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as error:
        return f"Error: bad regex {pattern!r}: {error}"
    try:
        root = paths.resolve(path, allow_root=True)
    except ValueError as error:
        return f"Error: {error}"
    files = [root] if root.is_file() else list(_walk(root))
    out: list[str] = []
    matches = 0
    files_hit = 0
    for file in files:
        rel = paths.display(file)
        if glob and not (fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(file.name, glob)):
            continue
        if paths.is_sensitive(file):
            continue
        try:
            if file.stat().st_size > 2 * 1024 * 1024:
                continue
            data = file.read_bytes()
            if b"\x00" in data[:4096]:
                continue
            text = data.decode("utf-8", errors="replace")
        except OSError:
            continue
        hits = [(i, line) for i, line in enumerate(text.split("\n"), 1) if regex.search(line)]
        if not hits:
            continue
        files_hit += 1
        tag = compute_tag(text)
        out.append(f"[{rel}#{tag}]")
        shown = hits[:20]
        for number, line in shown:
            out.append(f"{number}:{line[:GREP_LINE_CHARS]}")
            matches += 1
        _remember_seen(rel, tag, {n for n, _ in shown})
        if len(hits) > 20:
            out.append(f"  … {len(hits) - 20} more matches in this file")
        if files_hit >= GREP_MAX_FILES or matches >= GREP_MAX_MATCHES:
            out.append(f"(Stopped at {files_hit} files / {matches} matches — narrow the "
                       "pattern or path to see more.)")
            break
    if not out:
        return f"No matches for {pattern!r}" + (f" in {glob}" if glob else "") + "."
    return "\n".join(out)


# --------------------------------------------------------------------------- #
#  The tools                                                                   #
# --------------------------------------------------------------------------- #

def _d_write(args: dict) -> str:
    return f"write {args.get('path', 'a file')} in the workspace"


def _d_edit(args: dict) -> str:
    return f"edit {args.get('path', 'a file')} in the workspace"


TOOLS = [
    Tool(
        name="list_files",
        description=("List files in your workspace (or a folder in it), with sizes. "
                     "pattern filters by glob, e.g. *.py or src/**/*.ts."),
        args={"path": "folder, workspace-relative (default: whole workspace)",
              "pattern": "glob to filter by"},
        optional=frozenset({"path", "pattern"}),
        tier="read", func=list_files,
    ),
    Tool(
        name="read_file",
        description=("Read a file from your workspace as numbered lines under a "
                     "[path#TAG] header. Shows 300 lines by default; the footer "
                     "says how to continue. Read before you edit."),
        args={"path": "workspace-relative path",
              "offset": "first line to show (default 1)",
              "limit": f"how many lines (default {READ_DEFAULT_LINES}, max {READ_MAX_LINES})"},
        optional=frozenset({"offset", "limit"}),
        tier="read", func=read_file,
    ),
    Tool(
        name="grep",
        description=("Search workspace files with a regex. Matches come back as "
                     "numbered lines under [path#TAG] headers (usable by edit_lines)."),
        args={"pattern": "regular expression",
              "path": "folder or file to search (default: whole workspace)",
              "glob": "only files matching this glob, e.g. *.py",
              "ignore_case": "true for case-insensitive"},
        optional=frozenset({"path", "glob", "ignore_case"}),
        tier="read", func=grep,
    ),
    Tool(
        name="write_file",
        description=("Create a new file, or overwrite one completely, with the given "
                     "content. For changing part of an existing file use edit_lines. "
                     "A file too long for one reply (roughly 150+ lines): write_file "
                     "the first part, then append_file the rest, part by part. "
                     "ALWAYS write code this way: leave content OUT of the JSON and put the "
                     "file in a fenced block right after the call (```html … ```), verbatim. "
                     "Code inside a JSON string breaks on one unescaped quote; the fence "
                     "cannot (measured)."),
        args={"path": "workspace-relative path", "content": "the full file content"},
        tier="write", func=write_file, describe=_d_write,
    ),
    Tool(
        name="append_file",
        description=("Add text to the END of a file (created if missing). This is how "
                     "a long file is written in parts across several replies: each "
                     "part continues exactly where the previous one stopped. The text "
                     "may follow the call as a fenced block instead of a JSON string."),
        args={"path": "workspace-relative path", "content": "the text to append"},
        tier="write", func=append_file, describe=_d_write,
    ),
    Tool(
        name="edit_lines",
        description=("Edit a file by line numbers from your latest read_file/grep: "
                     "replace lines start..end (inclusive) with text. To insert "
                     "before line N use start=N, end=N-1; to delete, pass empty text. "
                     "tag is the 4-character TAG from the [path#TAG] header — it "
                     "proves your view is current. Returns the new tag and numbering."),
        args={"path": "workspace-relative path",
              "tag": "the TAG from the file's latest [path#TAG] header",
              "start": "first line to replace",
              "end": "last line to replace (start-1 to insert)",
              "text": "the new text for that range (may be several lines; empty deletes)"},
        optional=frozenset({"text"}),
        tier="write", func=edit_lines, describe=_d_edit,
    ),
    Tool(
        name="replace_in_file",
        description=("Edit a file by exact text: replace old_string (must occur once) "
                     "with new_string. Use when you have the exact snippet; "
                     "otherwise prefer edit_lines."),
        args={"path": "workspace-relative path",
              "old_string": "the exact text to replace",
              "new_string": "the replacement text",
              "replace_all": "true to replace every occurrence"},
        optional=frozenset({"replace_all"}),
        tier="write", func=replace_in_file, describe=_d_edit,
    ),
]
