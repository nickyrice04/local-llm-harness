"""Prompt loading: every prompt is a static .md file in this directory.

Prompts are never built by string concatenation in code (a discipline
borrowed from oh-my-pi): a prompt you can open and read is a prompt you can
review, diff, and improve without touching Python. Dynamic values are
substituted with explicit {{token}} markers — double braces, so JSON
examples inside prompts (single braces) never collide with substitution.
"""

# Path for locating the .md files next to this module.
from pathlib import Path

# This directory — the .md files live alongside the loader.
_PROMPT_DIR = Path(__file__).parent

# A tiny cache so repeated loads don't re-read the disk. Keyed by filename;
# values are the raw template text.
_cache: dict[str, str] = {}


def load(name: str, **tokens: str) -> str:
    """Load prompts/<name>.md and substitute {{token}} markers.

    Unknown markers are left in place (visible in output = visible bug),
    and unused kwargs are ignored — both choices favor loud, findable
    mistakes over silent ones.
    """
    if name not in _cache:
        _cache[name] = (_PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")
    text = _cache[name]
    # Straight replacement, one token at a time. No format() — its brace
    # syntax would fight the JSON examples inside the prompt bodies.
    for key, value in tokens.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text
