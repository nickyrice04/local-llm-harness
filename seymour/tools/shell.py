"""run_command — run a program in the workspace, CONFINED.

v1 shipped no exec tier at all ("removes the hardest safety problem
rather than gating it"). What changed is not the risk appetite but the
mechanism: macOS ships `sandbox-exec`, a kernel-enforced profile
language, and it is measured to work on this machine — writes outside
the granted folders fail with EPERM and the network is unreachable. So
the command runs inside a profile that:

    - allows reading the system (interpreters, libraries, the workspace),
    - DENIES reading secret-shaped home folders (.ssh, .gnupg, .aws,
      keychains) even though the rest of the disk is readable,
    - allows writing ONLY inside the workspace (plus /dev/null),
    - denies all networking (the model's web access is the guarded fetch
      tools, never a curl in a shell).

The semantics come from dsh's bash executor: exit code, timeout and
kill are reported INDEPENDENTLY (a process can trap the signal and exit
0 — a caller must never read a cut-short run as success), output is
merged as a terminal shows it, and when it is too long the model gets
the HEAD and the TAIL with the full text spilled to an artifact file it
can read back. Odysseus's subprocess tools contributed the process-group
kill and the streaming reader.

Off macOS (or if sandbox-exec ever vanishes) the tool still works but
says loudly, in every result, that it ran UNCONFINED.
"""

import asyncio
import os
import shutil
import signal
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from seymour.tools import paths

# The clock. Default long enough for a test suite, capped so a runaway
# never pins a run; the model may ask for more, up to the cap.
DEFAULT_TIMEOUT_S = 120
MAX_TIMEOUT_S = 600
# Output budget: head + tail inside the prompt, everything to the spill.
HEAD_CHARS = 1500
TAIL_CHARS = 4000
# Hard cap on what we even keep in memory from a chatty process.
MAX_CAPTURE_BYTES = 4 * 1024 * 1024
# Home folders no command may read even though the disk is otherwise
# readable — the secrets a scan would go for.
SECRET_HOME_DIRS = (".ssh", ".gnupg", ".aws", ".azure", ".config/gcloud",
                    "Library/Keychains", ".docker", ".kube", ".netrc")
# Environment variables handed to the child — an ALLOWLIST. The parent
# process holds API keys and tokens; none of them cross this line.
ENV_ALLOW = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "SHELL", "USER", "LOGNAME")

SANDBOX_EXEC = shutil.which("sandbox-exec") or (
    "/usr/bin/sandbox-exec" if os.path.exists("/usr/bin/sandbox-exec") else "")


@dataclass
class ExecResult:
    """One completed (or killed) run, every outcome its own field."""

    output: str                    # merged stdout+stderr as the terminal saw it
    exit_code: int | None          # None when killed by a signal
    timed_out: bool                # OUR timeout cut it short
    seconds: float
    truncated: bool                # output was cut for the prompt
    spill_path: str                # workspace-relative path of the full output
    sandboxed: bool


def _profile_text(workspace: Path) -> str:
    """The sandbox profile for one run. Written fresh per call because it
    embeds absolute paths; later rules override earlier ones, which is
    how the secret-folder denies sit on top of the blanket read allow."""
    home = Path.home()
    denies = "\n".join(
        f'(deny file-read* (subpath "{(home / d)}"))' for d in SECRET_HOME_DIRS)
    return f"""(version 1)
(deny default)
(allow process*)
(allow signal)
(allow sysctl-read)
(allow mach-lookup)
(allow ipc-posix*)
(allow file-read*)
{denies}
(allow file-write* (subpath "{workspace}"))
(allow file-write* (literal "/dev/null"))
(allow file-write* (regex #"^/dev/tty"))
(deny network*)
"""


def _child_env(workspace: Path) -> dict:
    """The child's environment: an allowlist of the parent's, plus a HOME
    and TMPDIR INSIDE the workspace so dotfiles and temp files have a
    legal place to land."""
    env = {key: os.environ[key] for key in ENV_ALLOW if key in os.environ}
    scratch = paths.scratch_dir()
    env.update({
        "HOME": str(scratch), "TMPDIR": str(scratch), "TERM": "dumb",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
        "NO_COLOR": "1", "SEYMOUR_WORKSPACE": str(workspace),
    })
    # The venv's python first on PATH, so "python" means the same
    # interpreter Seymour runs on (the one with the project's deps).
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "/usr/bin:/bin")
    return env


async def run(command: str, timeout_s: int = DEFAULT_TIMEOUT_S) -> ExecResult:
    """Run `command` under /bin/sh in the workspace, confined, bounded."""
    workspace = paths.workspace()
    timeout_s = max(1, min(int(timeout_s or DEFAULT_TIMEOUT_S), MAX_TIMEOUT_S))
    argv = ["/bin/sh", "-c", command]
    profile_path: Path | None = None
    sandboxed = bool(SANDBOX_EXEC)
    if sandboxed:
        # The profile lives in the scratch dir (inside the sandbox — the
        # child may not write it, but it never needs to).
        profile_path = paths.scratch_dir() / f"profile-{uuid.uuid4().hex[:8]}.sb"
        profile_path.write_text(_profile_text(workspace), encoding="utf-8")
        argv = [SANDBOX_EXEC, "-f", str(profile_path), *argv]

    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(workspace), env=_child_env(workspace),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,           # its own process group: kill takes children too
    )
    chunks: list[bytes] = []
    captured = 0
    timed_out = False

    async def _pump() -> None:
        """Read the merged stream to EOF, keeping at most MAX_CAPTURE_BYTES
        (a process printing gigabytes must not become our problem)."""
        nonlocal captured
        assert proc.stdout is not None
        while True:
            block = await proc.stdout.read(65536)
            if not block:
                return
            if captured < MAX_CAPTURE_BYTES:
                chunks.append(block[: MAX_CAPTURE_BYTES - captured])
            captured += len(block)

    pump = asyncio.create_task(_pump())
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_s)
    except asyncio.TimeoutError:
        timed_out = True
        _kill_group(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
    except asyncio.CancelledError:
        _kill_group(proc)                 # the run was cancelled: take the child with it
        raise
    finally:
        try:
            await asyncio.wait_for(pump, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pump.cancel()
        if profile_path is not None:
            profile_path.unlink(missing_ok=True)

    seconds = round(time.monotonic() - started, 2)
    text = b"".join(chunks).decode("utf-8", errors="replace")
    if captured > MAX_CAPTURE_BYTES:
        text += f"\n[output capture stopped at {MAX_CAPTURE_BYTES} bytes]"
    shown, truncated, spill = _bound_output(text)
    return ExecResult(output=shown, exit_code=proc.returncode,
                      timed_out=timed_out, seconds=seconds,
                      truncated=truncated, spill_path=spill, sandboxed=sandboxed)


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the child's whole process group (it was started as a
    session leader), tolerating an already-dead process."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _bound_output(text: str) -> tuple[str, bool, str]:
    """Keep the head and the tail for the prompt; spill the whole thing
    to an artifact file and say where. The tail is where a test runner
    puts its verdict; the head is where a compiler puts its first error."""
    if len(text) <= HEAD_CHARS + TAIL_CHARS + 200:
        return text, False, ""
    spill = paths.artifacts_dir() / f"cmd-{uuid.uuid4().hex[:8]}.log"
    spill.write_text(text, encoding="utf-8")
    omitted = len(text) - HEAD_CHARS - TAIL_CHARS
    shown = (text[:HEAD_CHARS]
             + f"\n\n[… {omitted} characters omitted — full output saved to "
             f"{paths.display(spill)}; read_file it if you need the middle …]\n\n"
             + text[-TAIL_CHARS:])
    return shown, True, paths.display(spill)


def render(result: ExecResult) -> str:
    """The result as the model reads it: verdict line first, then output."""
    if result.timed_out:
        verdict = (f"TIMED OUT after the limit and was killed "
                   f"(ran {result.seconds}s)")
    elif result.exit_code is None:
        verdict = f"killed by a signal ({result.seconds}s)"
    elif result.exit_code == 0:
        verdict = f"exit code 0 ({result.seconds}s)"
    else:
        verdict = f"exit code {result.exit_code} ({result.seconds}s)"
    lines = [f"[{verdict}]"]
    if not result.sandboxed:
        lines.append("[WARNING: sandbox-exec is unavailable — this command "
                     "ran UNCONFINED]")
    lines.append(result.output.rstrip() if result.output.strip() else "(no output)")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  The tool                                                                   #
# --------------------------------------------------------------------------- #

async def run_command(command: str, timeout_s: str | int = "") -> str:
    """Tool entry: run one shell command in the workspace."""
    command = (command or "").strip()
    if not command:
        return "Error: run_command needs a command, e.g. python -m pytest -q"
    if len(command) > 20000:
        return "Error: that command is too long — write it to a file and run the file."
    try:
        timeout = int(timeout_s) if str(timeout_s).strip() else DEFAULT_TIMEOUT_S
    except ValueError:
        timeout = DEFAULT_TIMEOUT_S
    result = await run(command, timeout)
    return render(result)


def _describe(args: dict) -> str:
    command = str(args.get("command", "")).strip().splitlines()
    first = command[0] if command else "a command"
    return f"run `{first[:80]}{'…' if len(first) > 80 else ''}` in the workspace"


from seymour.tools import Tool                      # noqa: E402  (registry type)

TOOLS = [
    Tool(
        name="run_command",
        description=(
            "Run a shell command inside your workspace (cwd = workspace) and "
            "return its output and exit code. Use it to run tests, scripts "
            "and builds: e.g. `python -m pytest -q`, `python script.py`, "
            "`node app.js`. The command runs in a sandbox: it can read the "
            "system and your workspace, write ONLY inside the workspace, and "
            "has NO network. Do not use it to edit files (use edit_file / "
            "write_file) or to fetch the web (use fetch_page)."),
        args={"command": "the shell command to run",
              "timeout_s": f"seconds before it is killed (default {DEFAULT_TIMEOUT_S}, max {MAX_TIMEOUT_S})"},
        optional=frozenset({"timeout_s"}),
        tier="exec",
        func=run_command,
        describe=_describe,
    ),
]
