"""Test bootstrap: isolate ALL state before any seymour module loads.

seymour.config reads the environment at import time, so the redirect must
happen first — hence this file, which pytest imports before the tests.
Every test run gets a throwaway data directory (database, soul, workspace),
and the real ~/.seymour is never touched.
"""

import os
import tempfile

# One temp directory per test session; TemporaryDirectory cleans up on GC.
_tmp = tempfile.TemporaryDirectory(prefix="seymour-test-")

# Point every stateful path into the sandbox BEFORE seymour.config loads.
os.environ["SEYMOUR_DATA_DIR"] = _tmp.name
# A nonexistent model path keeps anything engine-shaped inert in tests.
os.environ["SEYMOUR_MODEL_PATH"] = os.path.join(_tmp.name, "no-model.gguf")
