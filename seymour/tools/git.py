"""Git tools: git_overview, git_file_diff, git_hunk.

Cheap, and they make the agent's edits reviewable (oh-my-pi's set). All
three shell out to the system git through the same confined runner as
run_command — reads of the repository, writes only inside the workspace
(where .git lives), no network — so a `git fetch` cannot happen here by
construction. Nothing commits; the person does that.
"""

import re
import shutil
import subprocess

from seymour.tools import paths, shell


def _find_git() -> str:
    """The real git binary. /usr/bin/git on macOS is an xcrun shim that
    wants to write a cache file under /var/folders — outside the sandbox's
    writable set — and fails there intermittently ("couldn't create cache
    file … Operation not permitted", measured 2026-09-11). Resolve the
    real binary once, here, outside the sandbox."""
    for candidate in ("/opt/homebrew/bin/git", "/usr/local/bin/git",
                      "/Library/Developer/CommandLineTools/usr/bin/git",
                      "/Applications/Xcode.app/Contents/Developer/usr/bin/git"):
        if shutil.which(candidate):
            return candidate
    try:
        found = subprocess.run(["xcrun", "--find", "git"], capture_output=True, text=True, timeout=10).stdout.strip()
        if found:
            return found
    except (OSError, subprocess.SubprocessError):
        pass
    return shutil.which("git") or "git"


GIT = _find_git()


async def _git(args: str, timeout: int = 30) -> tuple[bool, str]:
    """Run one git command in the workspace; (ok, output)."""
    result = await shell.run(f"{GIT} {args}", timeout)
    # rstrip only: `status --short` puts the index column in char 1 and
    # the worktree column in char 2 — " M a.py" — and a strip would eat it.
    text = result.output.rstrip()
    if text.startswith("\n"):
        text = text.lstrip("\n")
    if result.exit_code != 0:
        if "not a git repository" in text.lower():
            return False, "Error: the workspace is not a git repository (no .git here). `git init` with run_command if you want one."
        return False, f"Error: git {args.split(' ')[0]} failed (exit {result.exit_code}):\n{text[-1500:]}"
    return True, text


async def git_overview() -> str:
    """Tool entry: branch, status and the last commits."""
    ok, branch = await _git("rev-parse --abbrev-ref HEAD")
    if not ok:
        return branch
    # Seymour's own bookkeeping folder is not the person's work.
    _, status = await _git("status --short -- . ':(exclude).seymour'")
    _, log = await _git("log --oneline -8")
    _, stat = await _git("diff --stat")
    lines = [f"branch: {branch}",
             "working tree:" if status else "working tree: clean",
             *status.splitlines()[:60]]
    if len(status.splitlines()) > 60:
        lines.append(f"  … {len(status.splitlines()) - 60} more entries")
    if stat:
        lines += ["", "unstaged changes:", *stat.splitlines()[-25:]]
    lines += ["", "recent commits:", *(log.splitlines() or ["(none yet)"])]
    return "\n".join(lines)


async def git_file_diff(path: str, staged: str | bool = False) -> str:
    """Tool entry: one file's diff against HEAD (or the index)."""
    try:
        target = paths.resolve(path)
    except ValueError as error:
        return f"Error: {error}"
    rel = paths.display(target)
    flag = "--cached " if str(staged).strip().lower() in ("true", "1", "yes") else ""
    ok, out = await _git(f"diff {flag}-U3 -- {_quote(rel)}")
    if not ok:
        return out
    if not out:
        ok2, tracked = await _git(f"ls-files --error-unmatch -- {_quote(rel)}")
        if not ok2:
            return f"{rel} is untracked (not in git yet) — the whole file is new."
        return f"{rel}: no {'staged ' if flag else ''}changes."
    return out


async def git_hunk(path: str, start: str | int, end: str | int = "") -> str:
    """Tool entry: the diff hunks that touch lines start..end of a file."""
    try:
        target = paths.resolve(path)
        first = int(str(start).strip())
        last = int(str(end).strip()) if str(end).strip() else first
    except (ValueError, TypeError) as error:
        return f"Error: {error}"
    rel = paths.display(target)
    ok, out = await _git(f"diff -U2 -- {_quote(rel)}")
    if not ok:
        return out
    if not out:
        return f"{rel}: no changes."
    header, hunks = _split_hunks(out)
    wanted = []
    for hunk in hunks:
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", hunk)
        if not m:
            continue
        h_start = int(m.group(1))
        h_end = h_start + int(m.group(2) or 1) - 1
        if h_end >= first and h_start <= last:
            wanted.append(hunk)
    if not wanted:
        return f"{rel}: no changes overlap lines {first}-{last} ({len(hunks)} hunk(s) elsewhere)."
    return "\n".join([header, *wanted])


def _split_hunks(diff: str) -> tuple[str, list[str]]:
    lines = diff.split("\n")
    header_end = next((i for i, l in enumerate(lines) if l.startswith("@@")), len(lines))
    header = "\n".join(lines[:header_end])
    hunks: list[str] = []
    current: list[str] = []
    for line in lines[header_end:]:
        if line.startswith("@@") and current:
            hunks.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        hunks.append("\n".join(current))
    return header, hunks


def _quote(rel: str) -> str:
    return "'" + rel.replace("'", "'\\''") + "'"


from seymour.tools import Tool                      # noqa: E402  (registry type)

TOOLS = [
    Tool(
        name="git_overview",
        description="The repository at a glance: branch, changed files, unstaged diff stat, recent commits.",
        args={}, tier="read", func=git_overview,
    ),
    Tool(
        name="git_file_diff",
        description="One file's diff against HEAD (staged=true: the index). Review what you changed before finishing.",
        args={"path": "workspace-relative path", "staged": "true for the staged diff"},
        optional=frozenset({"staged"}),
        tier="read", func=git_file_diff,
    ),
    Tool(
        name="git_hunk",
        description="Only the diff hunks that touch lines start..end of a file.",
        args={"path": "workspace-relative path", "start": "first line", "end": "last line (default: start)"},
        optional=frozenset({"end"}),
        tier="read", func=git_hunk,
    ),
]
