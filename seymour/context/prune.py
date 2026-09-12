"""Prune: blank results that no longer inform the next step.

oh-my-pi's rule, restated for Seymour's message shape: a tool result is
worth its tokens only while it is the model's freshest view of something.
A read of a file that was read again later is not; a "wrote a.py, tag
now #1F3C" that a later result for a.py superseded is not; the 16 KB
excerpt of a spilled log from twenty rounds ago is not, though its
pointer still is. Blanking those IN PLACE — the message stays, its body
becomes one bracketed line saying what was there and why it is gone —
keeps the call/result pairing intact and keeps the model honest about
what it once saw.

Pruning is gated by pressure (economy.py fires it only once the prompt
is large) because every change to an old message invalidates the prompt
cache from that point on. While the prompt is small the cache is worth
more than the tokens; once it is large, the tokens are.

The recent window is never pruned: the last K pairs are what the model
is working from right now.
"""

from seymour.context.spill import pointer_only
from seymour.guard import GUARD_CLOSE

# Tools whose success means the file on disk changed.
FILE_WRITERS = frozenset({"write_file", "append_file", "edit_lines", "replace_in_file"})
# Read-only tools whose long results age fast: a listing, a search, a page,
# a command's output. Older than the window and over TRIM_OVER chars, they
# keep a head and a note (rule R4). Reads are handled by R1 instead.
AGEING = frozenset({"grep", "list_files", "glob", "web_search", "fetch_page", "run_command",
                    "check_page", "job_output", "git_overview", "git_file_diff", "git_hunk"})
# R4's thresholds: results longer than this are trimmed to this head.
TRIM_OVER = 1_500
TRIM_HEAD = 600


def _meta(message: dict) -> dict:
    return message.get("_meta") or {}


def _later_results(convo: list[dict], index: int, path: str) -> list[dict]:
    """Every later RESULT message that concerns `path`."""
    return [_meta(m) for m in convo[index + 1:]
            if _meta(m).get("kind") == "result" and _meta(m).get("path") == path]


def decide(convo: list[dict], index: int) -> tuple[str, str] | None:
    """Should the result at `index` be pruned? Returns (rule, replacement
    text) or None. Pure: reads the convo, changes nothing."""
    meta = _meta(convo[index])
    if meta.get("kind") != "result" or meta.get("pruned"):
        return None
    tool = meta.get("tool") or ""
    path = meta.get("path") or ""
    ok = meta.get("ok", True)
    content = convo[index].get("content") or ""

    # R1 — a read superseded by a later read of the same file, or made stale
    # by a later change to it. The later result is the fresh view.
    if tool == "read_file" and path:
        later = _later_results(convo, index, path)
        if any(m.get("tool") == "read_file" for m in later):
            return "superseded_read", f"[read of {path} superseded by a later read of the same file]"
        if any(m.get("tool") in FILE_WRITERS and m.get("ok", True) for m in later):
            return "stale_read", f"[read of {path} is stale — the file was changed afterwards; re-read it if you need it]"

    # R2 — a successful write/edit whose tag a later result for the same
    # file has replaced: the file is on disk, the newer result has the tag.
    if tool in FILE_WRITERS and path and ok:
        if _later_results(convo, index, path):
            return "uneventful_write", f"[uneventful result elided: {tool} {path} succeeded; a later result for this file follows]"

    # R3 — a spilled excerpt older than the window: the pointer is enough.
    pointer = pointer_only(content)
    if pointer:
        return "spill_pointer", f"[{tool} result excerpt elided under context pressure]\n{pointer}"

    # R4 — an ageing read-only result: keep its head (headers, exit code,
    # the first matches) and say what was cut. The guard block is re-closed
    # so the untrusted marker pair stays balanced.
    if tool in AGEING and len(content) > TRIM_OVER:
        head = content[:TRIM_HEAD]
        return "trimmed", (f"{head}\n[… {len(content) - TRIM_HEAD:,} more characters of this older "
                           f"{tool} result elided under context pressure; call it again if you need it]"
                           + ("" if GUARD_CLOSE in head else f"\n{GUARD_CLOSE}"))
    return None


def prune(convo: list[dict], keep_recent_pairs: int) -> tuple[list[dict], list[dict]]:
    """Apply every rule to every result outside the recent window.

    Returns (new convo, actions) where each action is {"index", "rule",
    "tool", "path", "saved"} — the economy logs them. Messages are copied,
    never mutated: the run's earlier prompts stay reconstructible.
    """
    # Find the recent window: the last K result messages are protected,
    # and so is everything after the first of them.
    result_indexes = [i for i, m in enumerate(convo) if _meta(m).get("kind") == "result"]
    protected_from = result_indexes[-keep_recent_pairs] - 1 if len(result_indexes) >= keep_recent_pairs and keep_recent_pairs > 0 else len(convo)
    out = list(convo)
    actions: list[dict] = []
    for index in range(len(convo)):
        if index >= protected_from:
            break
        verdict = decide(out, index)
        if verdict is None:
            continue
        rule, replacement = verdict
        before = out[index]
        saved = len(before.get("content") or "") - len(replacement)
        if saved <= 0:
            continue                               # never prune into something longer
        out[index] = {**before, "content": replacement,
                      "_meta": {**_meta(before), "pruned": rule}}
        actions.append({"index": index, "rule": rule, "tool": _meta(before).get("tool"),
                        "path": _meta(before).get("path"), "saved": saved})
    return out, actions
