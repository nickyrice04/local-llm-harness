"""Command-line entry: .venv/bin/python -m evals.render <file> <outdir> [--steps "..."]"""

import argparse
import json
from pathlib import Path

from evals.render import render_sync
from seymour.verify.web import parse_steps

parser = argparse.ArgumentParser()
parser.add_argument("file")
parser.add_argument("outdir")
parser.add_argument("--steps", default="", help="interaction steps for an .html file (see seymour/verify/web.py)")
args = parser.parse_args()
print(json.dumps(render_sync(Path(args.file), Path(args.outdir), parse_steps(args.steps)), indent=1))
