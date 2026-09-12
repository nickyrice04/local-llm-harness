"""seymour.context — the context economy: spill, prune, compact.

Why this package exists (2026-09-11): the chat run was capped at twelve
tool calls because the harness could not SURVIVE a long run — every tool
result stayed in the prompt forever, oversized results were truncated in
silence, and nothing ever summarized. The cap and the missing economy
were one problem. This package is the economy; with it in place the cap
can go up (run_executor.CHAT_POLICY).

Three mechanisms, applied in this order and each one traceable:

    spill.py    a result over the inline budget is written IN FULL to a
                run artifact; the prompt keeps a head/tail excerpt and a
                pointer the model can read back with read_file. Nothing
                is lost (dsh's spill-policy is the reference).
    prune.py    older results that no longer inform the next step are
                blanked in place — a read superseded by a later read of
                the same file, a write result superseded by a later
                result for the same file, an old spilled excerpt shrunk
                to its pointer (oh-my-pi's ruleset is the reference).
    compact.py  when the prompt crosses a fraction of the loaded context,
                the OLDEST tool-pair-balanced range is summarized into a
                structured "what has happened so far" block and the last
                K turns stay verbatim. A call is never split from its
                result (dsh's tool-pairing lock bracket).

economy.py ties them together: it tags the run's messages with metadata
(`_meta`, stripped before the engine sees them), estimates the prompt's
size, decides when each mechanism fires, and reports every decision as a
RunEvent — traceability is Seymour's headline; a silent optimizer would
betray it.
"""

from seymour.context.economy import Economy, estimate_tokens, strip  # noqa: F401
from seymour.context.spill import INLINE_CHARS, apply as spill  # noqa: F401

__all__ = ["Economy", "INLINE_CHARS", "estimate_tokens", "spill", "strip"]
