"""Compact: summarize the oldest tool-pair-balanced range into one block.

When pruning is not enough — a run that has read forty files and run
thirty commands — the oldest part of the conversation is replaced by a
structured summary ("what has happened so far": decisions made, files
touched, facts established, what is still open) and the last K turns
stay verbatim. dsh's compaction-basic is the template; oh-my-pi's
snapcompact (bitmap-frame compression) was deliberately not followed —
clever, and wrong for a local harness whose prompt cache is the thing
being protected.

The lock bracket: a tool CALL is never separated from its RESULT. In
Seymour's message shape a pair is two adjacent messages (assistant call,
user result), so a cut is balanced exactly when the message before it is
not a call. `choose_range` only ever cuts there.

Pinned messages (the person's current request) are never summarized
away: they are lifted out of the range and re-inserted right after the
summary block, so the model always has the verbatim ask.
"""

import json

from seymour.prompts import load

# The transcript handed to the summarizer is bounded (it must fit the
# same context the run is struggling with). Head and tail of each long
# message, so a huge result does not crowd out the calls around it.
MESSAGE_EXCERPT = 1_800
TRANSCRIPT_MAX = 60_000


def _meta(message: dict) -> dict:
    return message.get("_meta") or {}


def _text(message: dict) -> str:
    """A message's content as text (image content arrays become their
    text parts plus a marker)."""
    content = message.get("content")
    if isinstance(content, list):
        parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
        images = sum(1 for p in content if isinstance(p, dict) and p.get("type") != "text")
        return "\n".join(parts) + (f"\n[{images} image(s)]" if images else "")
    return str(content or "")


def choose_range(convo: list[dict], keep_recent_pairs: int) -> tuple[int, int] | None:
    """The oldest range worth summarizing: [start, end) over `convo`.

    start is the first message after the system prompt; end is the cut
    before the K-th most recent pair's call (so K pairs stay verbatim),
    moved earlier if needed until it is balanced. None when there is
    nothing old enough to summarize (fewer than K+1 pairs, or a range
    of fewer than two messages — summarizing one message saves nothing).
    """
    if not convo:
        return None
    start = 1 if _meta(convo[0]).get("kind") == "system" or convo[0].get("role") == "system" else 0
    result_indexes = [i for i, m in enumerate(convo) if _meta(m).get("kind") == "result"]
    if len(result_indexes) <= keep_recent_pairs:
        return None
    # The K-th most recent result's call is the first message that must
    # stay; the cut lands just before it.
    end = result_indexes[-keep_recent_pairs] - 1 if keep_recent_pairs > 0 else len(convo)
    # Balanced cut: never right after a call (its result would be orphaned).
    while end > start and _meta(convo[end - 1]).get("kind") == "call":
        end -= 1
    if end - start < 2:
        return None
    return start, end


def transcript(convo: list[dict], start: int, end: int) -> str:
    """The range as a readable transcript for the summarizer."""
    lines: list[str] = []
    for message in convo[start:end]:
        meta = _meta(message)
        kind = meta.get("kind") or message.get("role")
        text = _text(message)
        if len(text) > MESSAGE_EXCERPT:
            half = MESSAGE_EXCERPT // 2
            text = text[:half] + f"\n[… {len(text) - MESSAGE_EXCERPT:,} chars omitted …]\n" + text[-half:]
        label = kind
        if kind == "call":
            label = f"tool call: {meta.get('tool')}" + (f" {meta.get('path')}" if meta.get("path") else "")
        elif kind == "result":
            label = f"tool result: {meta.get('tool')}" + (" (error)" if meta.get("ok") is False else "")
        lines.append(f"### {label}\n{text}\n")
    text = "\n".join(lines)
    if len(text) > TRANSCRIPT_MAX:
        text = text[:TRANSCRIPT_MAX // 2] + "\n\n[… middle of the transcript omitted …]\n\n" + text[-TRANSCRIPT_MAX // 2:]
    return text


def mechanical_summary(convo: list[dict], start: int, end: int) -> str:
    """The fallback when the model cannot summarize: a factual ledger of
    what happened, built from the metadata alone. Less useful than a real
    summary, never wrong, never empty."""
    calls: list[str] = []
    files: dict[str, str] = {}
    errors = 0
    for message in convo[start:end]:
        meta = _meta(message)
        if meta.get("kind") == "call":
            args = meta.get("args") or {}
            short = json.dumps({k: (v if len(str(v)) <= 80 else str(v)[:80] + "…") for k, v in args.items()},
                               ensure_ascii=False, default=str)
            calls.append(f"- {meta.get('tool')} {short}")
        elif meta.get("kind") == "result":
            if meta.get("ok") is False:
                errors += 1
            if meta.get("path") and meta.get("tool") in ("write_file", "append_file", "edit_lines", "replace_in_file"):
                files[meta["path"]] = "changed"
            elif meta.get("path") and meta.get("tool") == "read_file":
                files.setdefault(meta["path"], "read")
    return ("## Tool calls so far\n" + ("\n".join(calls) or "(none)")
            + "\n\n## Files touched\n" + ("\n".join(f"- {p}: {what}" for p, what in files.items()) or "(none)")
            + f"\n\n## Errors\n{errors} tool result(s) reported an error."
            + "\n\n## Still open\nUnknown — this summary was built mechanically because the model's summary failed; "
              "re-read what you need.")


def splice(convo: list[dict], start: int, end: int, summary: str, method: str) -> list[dict]:
    """Replace convo[start:end] with the summary block, re-inserting any
    pinned messages from the range right after it, in their order."""
    pinned = [m for m in convo[start:end] if _meta(m).get("pinned")]
    block = {"role": "user",
             "content": ("[Context compacted — the earlier part of this run was summarized. "
                         "What has happened so far:]\n\n" + summary.strip()
                         + "\n\n[End of summary. The verbatim recent turns follow. Files on disk are the "
                           "truth — re-read anything you need exact.]"),
             "_meta": {"kind": "summary", "method": method, "replaced": end - start}}
    return convo[:start] + [block] + pinned + convo[end:]


def summary_prompt(convo: list[dict], start: int, end: int, request: str) -> list[dict]:
    """The messages for the summarizer call (prompts/compact.md)."""
    return [{"role": "user", "content": load("compact", request=request or "(not recorded)",
                                              transcript=transcript(convo, start, end))}]
