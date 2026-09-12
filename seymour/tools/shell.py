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


# --------------------------------------------------------------------------- #
#  The persistent shell                                                        #
# --------------------------------------------------------------------------- #
# run_command is one-shot: `cd`, exported variables and an activated venv
# do not survive to the next call, and a model that types `cd app` then
# `pytest` is confused by the result. `shell` keeps ONE /bin/sh per run
# alive under the same sandbox profile (dsh's tool-bash-persistent):
# each command is written to its stdin followed by a sentinel line that
# carries the exit code, and the tool reads until the sentinel. State
# persists; confinement persists (the profile wraps the shell itself).
# The session ends with the run, on an idle timeout, or on a timeout
# that had to kill it (a hung command takes its shell with it — a new
# one starts on the next call, and the result says so).

import contextlib as _contextlib
from seymour.tools import context as _context

SHELL_IDLE_S = 900.0          # an untouched session closes after this
SHELL_DEFAULT_TIMEOUT_S = 120
SHELL_MAX_TIMEOUT_S = 600


@dataclass
class ShellSession:
    proc: asyncio.subprocess.Process
    profile_path: Path | None
    last_used: float
    lock: asyncio.Lock
    run: str
    sandboxed: bool


_sessions: dict[str, ShellSession] = {}


async def _open_session(run: str) -> ShellSession:
    workspace = paths.workspace()
    argv = ["/bin/sh"]
    profile_path: Path | None = None
    sandboxed = bool(SANDBOX_EXEC)
    if sandboxed:
        profile_path = paths.scratch_dir() / f"shell-profile-{uuid.uuid4().hex[:8]}.sb"
        profile_path.write_text(_profile_text(workspace), encoding="utf-8")
        argv = [SANDBOX_EXEC, "-f", str(profile_path), *argv]
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(workspace), env=_child_env(workspace),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, start_new_session=True)
    session = ShellSession(proc=proc, profile_path=profile_path, last_used=time.monotonic(),
                           lock=asyncio.Lock(), run=run, sandboxed=sandboxed)
    _sessions[run] = session
    return session


async def _close_session(run: str) -> None:
    session = _sessions.pop(run, None)
    if session is None:
        return
    if session.proc.returncode is None:
        _kill_group(session.proc)
        with _contextlib.suppress(Exception):
            await asyncio.wait_for(session.proc.wait(), timeout=3)
    if session.profile_path:
        session.profile_path.unlink(missing_ok=True)


async def close_all_shells(run: str | None = None) -> int:
    """End every session (of one run, or all): run end, app shutdown."""
    targets = [r for r in list(_sessions) if run is None or r == run]
    for r in targets:
        await _close_session(r)
    return len(targets)


async def shell_exec(command: str, timeout_s: int = SHELL_DEFAULT_TIMEOUT_S) -> tuple[ExecResult, bool]:
    """Run `command` in the calling run's persistent shell. Returns the
    result and whether a fresh session had to be started."""
    run = _context.run_id.get() or "default"
    fresh = False
    session = _sessions.get(run)
    if session is None or session.proc.returncode is not None or time.monotonic() - session.last_used > SHELL_IDLE_S:
        await _close_session(run)
        session = await _open_session(run)
        fresh = True
    async with session.lock:
        session.last_used = time.monotonic()
        marker = f"__SEYMOUR_DONE_{uuid.uuid4().hex[:10]}__"
        assert session.proc.stdin is not None and session.proc.stdout is not None
        # The command in its own group so a wrapper `{ ...; }` keeps the
        # shell's state (cd/export inside it still apply — it is not a
        # subshell), then the sentinel with the command's exit code.
        script = f"{command}\nprintf '\\n{marker} %s\\n' \"$?\"\n"
        started = time.monotonic()
        session.proc.stdin.write(script.encode("utf-8"))
        await session.proc.stdin.drain()
        chunks: list[bytes] = []
        captured = 0
        timed_out = False
        exit_code: int | None = None
        deadline = started + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                line = await asyncio.wait_for(session.proc.stdout.readline(), timeout=remaining)
            except asyncio.TimeoutError:
                timed_out = True
                break
            if not line:                                    # the shell died
                break
            text = line.decode("utf-8", errors="replace")
            if text.startswith(marker):
                with _contextlib.suppress(ValueError):
                    exit_code = int(text[len(marker):].strip() or "0")
                break
            if captured < MAX_CAPTURE_BYTES:
                chunks.append(line[: MAX_CAPTURE_BYTES - captured])
            captured += len(line)
        if timed_out:
            # A hung command cannot be separated from its shell: kill the
            # session; the next call starts a fresh one and says so.
            await _close_session(run)
        seconds = round(time.monotonic() - started, 2)
        text = b"".join(chunks).decode("utf-8", errors="replace")
        if text.endswith("\n"):
            text = text[:-1]
        shown, truncated, spill = _bound_output(text)
        return ExecResult(output=shown, exit_code=exit_code, timed_out=timed_out, seconds=seconds,
                          truncated=truncated, spill_path=spill, sandboxed=session.sandboxed), fresh


async def shell(command: str, timeout_s: str | int = "") -> str:
    """Tool entry: run a command in the run's persistent shell."""
    command = (command or "").strip()
    if not command:
        return "Error: shell needs a command"
    if len(command) > 20000:
        return "Error: that command is too long — write it to a file and run the file."
    try:
        timeout = max(1, min(int(str(timeout_s).strip() or SHELL_DEFAULT_TIMEOUT_S), SHELL_MAX_TIMEOUT_S))
    except ValueError:
        timeout = SHELL_DEFAULT_TIMEOUT_S
    result, fresh = await shell_exec(command, timeout)
    text = render(result)
    if result.timed_out:
        text += "\n[the shell session was ended by the timeout; the next shell call starts a fresh one (cwd and variables reset)]"
    elif fresh:
        text = "[new shell session — cwd is the workspace]\n" + text
    return text


def _d_shell(args: dict) -> str:
    first = str(args.get("command", "")).strip().splitlines()
    first = first[0] if first else "a command"
    return f"run `{first[:80]}{'…' if len(first) > 80 else ''}` in the persistent shell"


TOOLS.append(Tool(
    name="shell",
    description=("Run a command in a PERSISTENT shell session: cd, exported variables and an "
                 "activated venv carry over to your next shell call (run_command forgets them). "
                 "Same sandbox: workspace writes only, no network. Use it for multi-step "
                 "sessions; run_command for one-offs."),
    args={"command": "the shell command to run",
          "timeout_s": f"seconds before it is killed (default {SHELL_DEFAULT_TIMEOUT_S}, max {SHELL_MAX_TIMEOUT_S})"},
    optional=frozenset({"timeout_s"}),
    tier="exec", func=shell, describe=_d_shell,
))
