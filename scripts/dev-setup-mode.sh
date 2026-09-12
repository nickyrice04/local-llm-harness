#!/bin/bash
# Run Seymour in SETUP MODE for UI development: point the model path at a
# file that doesn't exist, so the backend boots instantly without loading
# 35 GB of weights. Chat answers 503; everything else works.
cd "$(dirname "$0")/.."                       # repo root, wherever called from
export SEYMOUR_MODEL_PATH="/nonexistent/setup-mode.gguf"
# 8765 rather than 8000: the dev preview must not collide with whatever
# else the machine runs on common ports.
exec .venv/bin/python -m uvicorn seymour.app:app --host 127.0.0.1 --port 8765
