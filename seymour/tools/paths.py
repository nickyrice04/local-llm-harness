"""Workspace confinement — the sandbox boundary every file tool obeys.

The workspace (~/.seymour/workspace) is the ONLY directory the model's
file tools may touch, and the only place run_command may write. The
rules live here once so no tool re-implements them:

- Paths are workspace-relative. An absolute path INSIDE the workspace is
  accepted too (models echo the paths they were shown); anything that
  resolves outside is refused with a readable message. `resolve()`
  collapses ../ tricks and symlinks before the containment check.
- A short denylist of secret-shaped names (.env, private keys…) is
  refused for reading AND writing even inside the workspace — defense in
  depth for a folder a person might drop things into.
- Tool output never enumerates the noise directories (.git, venvs,
  node_modules) or Seymour's own bookkeeping folder.
"""

from pathlib import Path

from seymour.config import settings

# Files no tool reads or writes, wherever they sit (matched by name).
SENSITIVE_NAMES = frozenset({
    ".env", ".env.local", ".env.production", ".netrc", ".npmrc", ".pypirc",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials", "credentials.json",
})
# …and by suffix.
SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keychain")

# Directories listings and searches skip: build noise and version-control
# internals. Seymour's own artifact folder is skipped so spilled command
# output doesn't appear as "your files".
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".next",
    ".cache", ".seymour",
})


def workspace() -> Path:
    """The sandbox root, resolved (symlinks collapsed) — comparisons
    below are between resolved paths only."""
    return settings.workspace_dir.resolve()


def artifacts_dir() -> Path:
    """Where oversized tool output spills in full (inside the sandbox, so
    the model can read_file it back — dsh's spill-file idea)."""
    path = workspace() / ".seymour" / "artifacts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def scratch_dir() -> Path:
    """A private TMPDIR/HOME for commands — inside the sandbox, so a
    program that writes dotfiles or temp files has somewhere legal."""
    path = workspace() / ".seymour" / "scratch"
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_sensitive(path: Path) -> bool:
    """Secret-shaped by name or suffix?"""
    name = path.name.lower()
    return name in SENSITIVE_NAMES or name.endswith(SENSITIVE_SUFFIXES)


def resolve(relative: str, *, allow_root: bool = False) -> Path:
    """Resolve a model-supplied path INSIDE the workspace, or raise
    ValueError with a message the model can act on.

    `allow_root` lets "" / "." name the workspace itself (listings);
    file tools leave it False so an empty path is a readable error.
    """
    raw = (relative or "").strip()
    # Models sometimes prefix the folder they were told about; strip the
    # common forms so "workspace/notes.md" means "notes.md".
    for prefix in ("workspace/", "./"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
    if not raw or raw == ".":
        if allow_root:
            return workspace()
        raise ValueError("path is required (workspace-relative, e.g. notes/todo.md)")
    root = workspace()
    candidate = Path(raw)
    target = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if target != root and not target.is_relative_to(root):
        raise ValueError(
            f"path escapes the workspace: {relative!r}. Only files inside "
            f"your workspace are reachable; use a relative path.")
    if is_sensitive(target):
        raise ValueError(f"refused: {target.name} looks like a secret and is off limits")
    # A path THROUGH a skipped/bookkeeping dir is still allowed (the model
    # may need .seymour/artifacts/…); only secret names are refused.
    return target


def display(path: Path) -> str:
    """A path as the model should see it: workspace-relative."""
    try:
        return str(path.resolve().relative_to(workspace())) or "."
    except ValueError:
        return str(path)
