---
name: code-fix-loop
description: Make failing tests pass: run the tests, read the failure, edit only what is wrong with edit_lines, rerun until the exit code is 0; never edit the tests themselves.
---

# Run, read, fix, rerun

1. `run_command python -m pytest -q` (or the project's test command). Read the `[exit code N]` line and the assertion text — it names the file and the expectation.
2. `read_file` the module under test. Note the `[path#TAG]` and the line numbers; find the root cause (off-by-one, wrong divisor, wrong branch, wrong return).
3. Fix with `edit_lines(path, TAG, start, end, text)` on exactly those lines (or `replace_in_file` for a unique snippet). One cause, one edit.
4. Rerun the SAME command. Repeat until exit code 0 and `passed` appears. Do not edit test files unless the task says the test is wrong.
5. Report the line that changed and the final summary line of the test run.
