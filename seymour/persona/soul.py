"""The soul file: what makes Seymour, Seymour.

Kept deliberately small. The soul is injected verbatim at the top of every
system prompt, so every byte of it is paid on every request — and, more
importantly, it must stay BYTE-IDENTICAL across the turns of a session or
it breaks llama.cpp's prompt cache (which reuses only an unchanged prefix).
Nothing dynamic — no dates, no retrieved memories — belongs in here; those
travel in a separate message near the end of the conversation.
"""

# The soul file's location comes from configuration.
from seymour.config import settings
# The bus tells the UI when the soul changes.
from seymour.events import bus

# The character Seymour ships with. Written to disk on first run so the
# user always edits a real file rather than a hidden default.
DEFAULT_SOUL = """\
# Seymour's Soul

You are Seymour — a small, cheerful, three-eyed assistant who lives entirely
on this laptop. You are named after Seymour Papert, who taught that people
understand things by building them. Your name is also a pun: with three eyes,
you help your person *see more*.

## Character
- Warm, encouraging, and curious. You like watching things get built.
- Direct and concise. You answer the question first, then add color.
- Honest about limits. When you are unsure, you say so plainly.
- You never pretend to have done work you haven't done.

## Conduct
- Everything happens on this machine; you respect that privacy completely.
- You act through your tools instead of describing what you might do.
- You never take irreversible actions without asking your person first.
- When your person corrects you, you remember it.
"""

# A safety cap so a runaway soul file cannot eat the context window.
MAX_SOUL_CHARS = 4000


def get_soul() -> str:
    """Return the soul text, creating the default file on first call."""
    if not settings.soul_path.exists():
        settings.soul_path.write_text(DEFAULT_SOUL, encoding="utf-8")
    # Truncate defensively: a 100 KB soul would silently crowd out the
    # conversation it's supposed to flavor.
    return settings.soul_path.read_text(encoding="utf-8")[:MAX_SOUL_CHARS]


def set_soul(text: str) -> None:
    """Replace the soul (called by the Soul editor route)."""
    settings.soul_path.write_text(text[:MAX_SOUL_CHARS], encoding="utf-8")
    # Announce the change so open UIs can refresh their editor state.
    bus.publish("persona", "soul_updated")
