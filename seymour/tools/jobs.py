"""Background jobs: run_in_background / job_output / job_kill.

Installs, servers and builds blocked a run until 2026-09-11 — a dev
server that must stay up while a page is tested was impossible. dsh's
`packages/jobs` is the reference: start → a job id; output is read
CONSUMINGLY (each read returns what arrived since the last one, so the
model never re-reads a log it has seen); kill is explicit; completion
is a fact in the next job_output.

Jobs run under the same sandbox-exec profile as run_command, with one
difference: LOCAL networking is allowed (bind and connect on localhost)
so a dev server can serve and a test can hit it. The internet stays
closed — the model's web access is the guarded fetch tools.

Every job's output goes to a log file in the artifacts folder, so the
whole thing is readable with read_file and survives the job. Jobs die
with the app (guard.py's rule for engines, applied here through
process groups) and are capped per run.
"""

import asyncio
import os
import signal
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from seymour.tools import context, paths, shell

MAX_JOBS = 8                  # live jobs at once, process-wide
MAX_LOG_BYTES = 8 * 1024 * 1024
OUTPUT_CHARS = 6_000          # per job_output read
DEFAULT_WAIT_S = 0
MAX_WAIT_S = 120


@dataclass
class Job:
    id: str
    command: str
    proc: asyncio.subprocess.Process
    log_path: Path
    started: float = field(default_factory=time.monotonic)
    read_offset: int = 0      # consuming reads: where the last read stopped
    exit_code: int | None = None
    profile_path: Path | None = None
    run: str = ""

    @property
    def alive(self) -> bool:
        return self.proc.returncode is None


_jobs: dict[str, Job] = {}


def _local_network_profile(workspace: Path) -> str:
    """run_command's profile plus localhost networking."""
    base = shell._profile_text(workspace).rstrip()
    assert base.endswith("(deny network*)")
    return base[: -len("(deny network*)")] + (
        "(deny network*)\n"
        '(allow network-bind (local ip "localhost:*"))\n'
        '(allow network-inbound (local ip "localhost:*"))\n'
        '(allow network-outbound (remote ip "localhost:*"))\n'
        '(allow network-outbound (remote unix-socket))\n')


async def start(command: str) -> Job:
    """Launch `command` detached, output to a log file."""
    workspace = paths.workspace()
    log_path = paths.artifacts_dir() / f"job-{uuid.uuid4().hex[:8]}.log"
    argv = ["/bin/sh", "-c", command]
    profile_path: Path | None = None
    if shell.SANDBOX_EXEC:
        profile_path = paths.scratch_dir() / f"job-profile-{uuid.uuid4().hex[:8]}.sb"
        profile_path.write_text(_local_network_profile(workspace), encoding="utf-8")
        argv = [shell.SANDBOX_EXEC, "-f", str(profile_path), *argv]
    log = open(log_path, "ab")
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(workspace), env=shell._child_env(workspace),
            stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True)
    finally:
        log.close()
    job = Job(id=uuid.uuid4().hex[:8], command=command, proc=proc, log_path=log_path,
              profile_path=profile_path, run=context.run_id.get())
    _jobs[job.id] = job
    asyncio.create_task(_reap(job))
    return job


async def _reap(job: Job) -> None:
    """Record the exit code when the process ends; tidy the profile."""
    try:
        job.exit_code = await job.proc.wait()
    finally:
        if job.profile_path:
            job.profile_path.unlink(missing_ok=True)


def _read_new(job: Job, limit: int = OUTPUT_CHARS, consume: bool = True) -> tuple[str, int]:
    """New output since the last read (consuming, unless `consume` is
    False — the start notice peeks so the first job_output still shows
    it), bounded; returns (text, bytes still unread after this call)."""
    try:
        size = job.log_path.stat().st_size
        with open(job.log_path, "rb") as handle:
            handle.seek(job.read_offset)
            data = handle.read(limit)
    except OSError:
        return "", 0
    if consume:
        job.read_offset += len(data)
    return data.decode("utf-8", errors="replace"), max(size - job.read_offset - (0 if consume else len(data)), 0)


def _status_line(job: Job) -> str:
    age = round(time.monotonic() - job.started, 1)
    if job.alive:
        return f"[job {job.id} running · {age}s · `{job.command[:80]}`]"
    if job.exit_code is None:
        return f"[job {job.id} killed by a signal after {age}s]"
    return f"[job {job.id} exited with code {job.exit_code} after {age}s]"


async def run_in_background(command: str) -> str:
    """Tool entry: start a job."""
    command = (command or "").strip()
    if not command:
        return "Error: run_in_background needs a command, e.g. python -m http.server 8000"
    live = [j for j in _jobs.values() if j.alive]
    if len(live) >= MAX_JOBS:
        return (f"Error: {MAX_JOBS} jobs are already running — job_kill one first: "
                + ", ".join(f"{j.id} ({j.command[:30]})" for j in live))
    job = await start(command)
    await asyncio.sleep(0.3)                     # long enough to catch an immediate failure
    head, _ = _read_new(job, 1500, consume=False)
    return (f"{_status_line(job)}\nlog: {paths.display(job.log_path)}\n"
            "Use job_output(job_id) to read new output (it returns only what you have not seen); "
            "job_kill(job_id) to stop it. Local network is allowed for jobs (a server on localhost works)."
            + (f"\n--- first output ---\n{head.rstrip()}" if head.strip() else ""))


async def job_output(job_id: str, wait_s: str | int = "") -> str:
    """Tool entry: consuming read of a job's new output."""
    job = _jobs.get((job_id or "").strip())
    if job is None:
        known = ", ".join(_jobs) or "none"
        return f"Error: no job {job_id!r} (known: {known})"
    try:
        wait = max(0, min(int(str(wait_s).strip() or DEFAULT_WAIT_S), MAX_WAIT_S))
    except ValueError:
        wait = DEFAULT_WAIT_S
    # Optionally wait for new output or exit — a test that needs the
    # server to say "listening" first asks for a few seconds.
    deadline = time.monotonic() + wait
    while wait and job.alive and time.monotonic() < deadline:
        try:
            if job.log_path.stat().st_size > job.read_offset:
                break
        except OSError:
            break
        await asyncio.sleep(0.25)
    text, remaining = _read_new(job)
    lines = [_status_line(job)]
    if text.strip():
        lines.append(text.rstrip())
    else:
        lines.append("(no new output)")
    if remaining:
        lines.append(f"[{remaining:,} more bytes unread — call job_output again]")
    return "\n".join(lines)


async def job_kill(job_id: str) -> str:
    """Tool entry: stop a job (its whole process group)."""
    job = _jobs.get((job_id or "").strip())
    if job is None:
        return f"Error: no job {job_id!r}"
    if not job.alive:
        return f"{_status_line(job)} — already finished."
    try:
        os.killpg(job.proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        await asyncio.wait_for(job.proc.wait(), timeout=3)
    except asyncio.TimeoutError:
        shell._kill_group(job.proc)
        await asyncio.wait_for(job.proc.wait(), timeout=3)
    text, _ = _read_new(job, 2000)
    return f"{_status_line(job)}" + (f"\n--- last output ---\n{text.rstrip()}" if text.strip() else "")


async def kill_all(run: str | None = None) -> int:
    """Stop every live job (of one run, or all): app shutdown, run end."""
    count = 0
    for job in list(_jobs.values()):
        if run is not None and job.run != run:
            continue
        if job.alive:
            shell._kill_group(job.proc)
            count += 1
    return count


def _d_bg(args: dict) -> str:
    return f"run `{str(args.get('command', ''))[:80]}` in the background"


from seymour.tools import Tool                      # noqa: E402  (registry type)

TOOLS = [
    Tool(
        name="run_in_background",
        description=("Start a long-running command (a dev server, an install, a build, a watcher) "
                     "WITHOUT waiting for it. Returns a job id. Local network is allowed, so a server "
                     "on localhost works; the internet is not. Read its output with job_output, "
                     "stop it with job_kill. For commands that finish in seconds use run_command."),
        args={"command": "the shell command to start"},
        tier="exec", func=run_in_background, describe=_d_bg,
    ),
    Tool(
        name="job_output",
        description=("Read a background job's NEW output (only what you have not seen yet) and "
                     "whether it is still running or how it exited. wait_s waits up to that many "
                     "seconds for new output first."),
        args={"job_id": "the id run_in_background returned",
              "wait_s": f"seconds to wait for new output (default {DEFAULT_WAIT_S}, max {MAX_WAIT_S})"},
        optional=frozenset({"wait_s"}),
        tier="read", func=job_output,
    ),
    Tool(
        name="job_kill",
        description="Stop a background job (and everything it started).",
        args={"job_id": "the id run_in_background returned"},
        tier="exec", func=job_kill,
    ),
]
