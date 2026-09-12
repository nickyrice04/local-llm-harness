---
name: code-fix-loop
description: Make failing tests pass the honest way — read the failure first, reproduce it, find the cause (often not where the symptom is), make the minimal fix, rerun, and never edit a test to make it pass.
---

# Read, reproduce, fix, rerun

The rule that matters: **the failure tells you where the symptom is, not
where the cause is.** A stack trace ending in `parser.py` can be caused
by `loader.py` handing it the wrong thing. Read before you edit.

## The loop

1. **Run the tests first, exactly as the project does.**
   `run_command python -m pytest -q` (or `npm test`, or the command the
   task names). Read the `[exit code N]` line and the FULL assertion or
   traceback: which test, which file and line raised, what was expected,
   what was got.
2. **Reproduce the one failure in isolation.**
   `run_command python -m pytest -q test_x.py::test_name -x` — and, when
   the trace crosses files, print the offending value:
   `run_command python -c "from pkg import thing; print(thing(...))"`.
3. **Read the code path, not just the last frame.** `read_structure` the
   module(s) in the trace, then `read_file` the functions on the path
   from the test's call into the failing line. Ask: where does the wrong
   value FIRST appear? That is the cause. Use `grep` to find every caller
   of a function before changing its behaviour.
4. **Make the minimal fix** with `edit_lines(path, TAG, start, end, text)`
   on exactly the lines that are wrong (or `replace_in_file` for a unique
   snippet). One cause, one edit. Do not refactor, rename or "improve"
   nearby code — every extra change is a way to break another test.
5. **Rerun the SAME full command** from step 1. All green (exit code 0,
   `passed`, no `failed`/`error`)? If a different test now fails, your
   fix changed behaviour something else relied on: read that test, then
   reconsider the cause (step 3), don't patch the new symptom.
6. Optional but cheap: `git_file_diff` on what you changed and read it as
   a reviewer would. Is every line necessary?

## Never

- Never edit a test file to make it pass, weaken an assertion, add a
  `skip`, or wrap the failing call in `try/except`. If you believe the
  TEST is wrong, say so in your report with the evidence and stop.
- Never claim the tests pass without the tool result that shows `exit
  code 0` on the full run.
- Never "fix" by special-casing the test's exact input.

## Report

The final message names: the failing test(s), the root cause (file and
line, one sentence), what changed (file and line range), and the final
summary line of the passing run.
