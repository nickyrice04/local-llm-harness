---
name: refactor-multifile
description: Move duplicated code into one module and update every caller across files; grep first, read each file before editing, then re-grep and run the tests.
---

# Refactors that leave no stragglers

1. `grep` the duplicated name across the workspace: every definition and every call site with line numbers.
2. Decide the single home (an existing shared module if one fits, else a new one such as `app/env.py`).
3. `read_file` each file before editing. In the home file keep one definition; in every other file delete the copy with `edit_lines` and add `from <module> import <name>` at the top, removing imports that are now unused.
4. Re-`grep`: exactly one definition remains and every caller imports it.
5. `run_command python -m pytest -q` (or `python -c "import app.main"` when there are no tests) and fix any ImportError.
6. Report the files touched, one line each.
