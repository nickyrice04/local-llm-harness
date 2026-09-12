"""The child guard: an engine server that dies with the app, always.

    python -m seymour.engine.guard <parent-pid> -- <server command...>

Why this exists (measured three times on this project, 2026-09-02/03):
an engine child outlived the Seymour process that launched it — once a
llama-server, once an mlx-vlm benchmark server, once an mlx-lm server
after the app was killed hard by the preview tool. Each orphan held
28-35 GB of unified memory and a port; the next app instance either
adopted it blind (no way to know its flags) or failed to bind. macOS has
no PR_SET_PDEATHSIG, so the guard is the portable answer: a tiny process
that launches the real server, then watches the parent and takes the
server down the moment the parent is gone.

Rules of the guard:
  - it forwards SIGTERM/SIGINT to the server and mirrors the server's
    exit code, so the adapter's "dead child" check and stop() both keep
    working unchanged (they talk to the guard, which behaves like the
    server would);
  - it polls the parent every second with signal 0 — cheap, and a
    second of orphan life is nothing next to a 28 GB leak;
  - when the parent is gone it sends SIGTERM, waits up to 10 s, then
    SIGKILL, and exits non-zero (there is nobody left to read the code,
    but a log line says why).
"""

import os
import signal
import subprocess
import sys
import time


def guarded(command: list[str]) -> list[str]:
    """Wrap a server command so it runs under this guard, watching the
    CURRENT process (the app). Used by every engine adapter's launch."""
    return [sys.executable, "-m", "seymour.engine.guard", str(os.getpid()), "--", *command]


def _parent_alive(pid: int) -> bool:
    """Signal 0 asks the kernel whether the process exists (no signal sent)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                    # exists, just not ours to signal
    return True


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] != "--":
        sys.stderr.write("usage: guard <parent-pid> -- <command...>\n")
        return 2
    parent = int(argv[0])
    command = argv[2:]
    server = subprocess.Popen(command)

    def forward(signum, _frame):
        # The app asked us to stop: pass it on; the wait loop below then
        # mirrors the server's exit.
        try:
            server.send_signal(signum)
        except ProcessLookupError:
            pass
    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)

    while True:
        code = server.poll()
        if code is not None:
            return code                # the server ended on its own: mirror it
        if not _parent_alive(parent):
            sys.stderr.write(f"guard: parent {parent} is gone — stopping the server\n")
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
            return 3
        time.sleep(1.0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
