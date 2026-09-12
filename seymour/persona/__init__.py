"""The persona layer: Seymour's "soul".

One Markdown file on disk defines Seymour's character. The user can read and
edit it — in the app's Soul tab or in any text editor — and the file is the
single server-side source of truth (a lesson from the reference
implementation, whose browser-stored personas were invisible to the server).
"""

# Re-export the public surface.
from seymour.persona.soul import get_soul, set_soul  # noqa: F401
