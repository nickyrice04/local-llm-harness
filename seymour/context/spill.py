"""Spill: an oversized tool result goes to disk in full, not into the bin.

Before this module, `tools.bounded` cut any result over 8,000 characters
and appended "[result truncated …]". Honest, but lossy: the model could
never see the middle of a long grep, a long test log or a long page. Now
the WHOLE text is written to a run artifact inside the workspace (so
read_file can open it — the sandbox boundary is unchanged) and the
prompt keeps a head, a tail and the pointer. The excerpt is what the
model reads by default; the pointer is how it gets the rest.

The split favours the head (2/3) because a result's opening usually
carries its structure (headers, the first matches) while the tail
carries its verdict (exit codes, the last error) — both matter, the
head slightly more. dsh splits evenly; the asymmetry here is measured
on test logs, where the failure summary sits at the very end and a
short tail already holds it.

Exempt: `read_file`. The file IS the artifact, and it bounds itself by
line count and bytes with an exact-next-action footer (offset=N) — a
spill of a read would be a copy of a file that already exists (dsh
skips `read` for the same reason: read → spill → read again is a loop).
"""

import re
import uuid

from seymour.tools import paths

# The inline budget: how much of any one result may enter the prompt
# verbatim. ~4k tokens at 4 chars/token. Larger than the old hard cap
# because the economy can now afford it — old results are pruned and
# compacted instead of sitting in the prompt forever.
INLINE_CHARS = 16_000
# How the excerpt splits the inline budget between head and tail (see
# the module docstring for why the head gets more).
HEAD_FRACTION = 2 / 3
# Room reserved for the pointer/notice lines inside the inline budget,
# so an excerpt plus its notice never exceeds INLINE_CHARS.
NOTICE_ROOM = 400
# Tools whose results are never spilled (they bound themselves and the
# spill would duplicate a file that already exists).
EXEMPT = frozenset({"read_file"})
# The pointer line's shape, so prune.py can recognise a spilled result
# and shrink the excerpt down to just this line under pressure.
POINTER = re.compile(r"^\[spilled: .*?\]$", re.MULTILINE)


def apply(name: str, result: str, run_id: str = "", inline: int = INLINE_CHARS) -> tuple[str, dict | None]:
    """Return (text for the prompt, spill info or None).

    `text` is the result unchanged when it fits the inline budget or the
    tool is exempt. Otherwise the full result is written to
    `.seymour/artifacts/spill-<run>-<id>-<tool>.txt` and `text` is the
    head + tail excerpt with the pointer; `info` names the file and the
    true size so the caller can log it. A spill that cannot be written
    (a read-only disk, a vanished workspace) falls back to the old
    honest truncation rather than failing the tool call.
    """
    if name in EXEMPT or len(result) <= inline:
        return result, None
    # The artifact name carries the run (first 8 chars), a short id and
    # the tool, so a person browsing .seymour/artifacts can tell them apart.
    stem = f"spill-{(run_id or 'run')[:8]}-{uuid.uuid4().hex[:6]}-{name}"
    try:
        target = paths.artifacts_dir() / f"{stem}.txt"
        target.write_text(result, encoding="utf-8")
        rel = paths.display(target)
    except OSError:
        # The old behaviour, kept as the fallback: cut and say so.
        return (result[:inline] + f"\n[result truncated at {inline} of {len(result)} characters; "
                "the spill file could not be written]"), None
    budget = max(inline - NOTICE_ROOM, 200)
    head_n = int(budget * HEAD_FRACTION)
    tail_n = budget - head_n
    head, tail = result[:head_n], result[-tail_n:]
    # Line counts let the model ask for the exact middle: read_file takes
    # offset/limit in lines, so the pointer speaks in lines too.
    total_lines = result.count("\n") + 1
    head_lines = head.count("\n") + 1
    tail_lines = tail.count("\n") + 1
    omitted = max(total_lines - head_lines - tail_lines, 0)
    text = (f"{head}\n\n[… {len(result) - head_n - tail_n:,} characters / ~{omitted} lines omitted here …]\n\n{tail}\n"
            f"[spilled: the full {len(result):,}-character result ({total_lines} lines) is in {rel} — "
            f"read_file it with offset={head_lines + 1} to see the omitted middle]")
    return text, {"path": rel, "chars": len(result), "lines": total_lines, "inline_chars": len(text)}


def pointer_only(text: str) -> str | None:
    """The pointer line of a spilled excerpt, or None when `text` is not
    one — prune.py shrinks an old excerpt to this under pressure."""
    match = POINTER.search(text)
    return match.group(0) if match else None
