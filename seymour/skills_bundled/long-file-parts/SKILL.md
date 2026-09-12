---
name: long-file-parts
description: Write files longer than about 150 lines in parts with write_file then append_file, each part well under the reply cap, and check the seams by reading the whole file back.
---

# Long files in parts

A tool call cut off by the reply cap is NOT executed; the result says so. Do not retry the same oversized call.

1. Split at natural boundaries (head+CSS / body / script; class by class; section by section) into parts of at most ~120 lines.
2. `write_file` part 1; `append_file` each following part. Every part starts exactly where the previous one stopped: no repeated lines, no half statements.
3. After the last part, `read_file` the whole file (use `offset` to page) and check the seams: nothing duplicated or missing, brackets and tags balanced, every name used is defined somewhere in the file.
4. Only then run or verify it.
