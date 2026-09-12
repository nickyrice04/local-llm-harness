"""Run Seymour in SETUP MODE for UI development.

Points the model path at a file that doesn't exist, so the backend boots
instantly without loading 35 GB of weights: chat answers 503, everything
else (views, theming, onboarding, models tab) works. A Python twin of
dev-setup-mode.sh, for launchers that can only exec an interpreter.
"""

import os
from pathlib import Path

import uvicorn

# The nonexistent path IS the feature: app.py sees it missing and boots
# in setup mode instead of launching llama-server.
os.environ["SEYMOUR_MODEL_PATH"] = "/nonexistent/setup-mode.gguf"
# Run from the repo root so static/ resolves, wherever we were launched.
os.chdir(Path(__file__).resolve().parent.parent)

# 8765: the dev-preview port (must match .claude/launch.json).
uvicorn.run("seymour.app:app", host="127.0.0.1", port=8765)
