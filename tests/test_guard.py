"""The child guard: an engine server must never outlive the app."""

import os
import signal
import subprocess
import sys
import time

GUARD = [sys.executable, "-m", "seymour.engine.guard"]


def _wait_gone(proc: subprocess.Popen, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.1)
    return False


def test_guard_stops_the_server_when_the_parent_dies():
    # A stand-in "parent": a process we can kill on cue.
    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    # The guarded "server": would run for a minute if nobody stopped it.
    guard = subprocess.Popen(GUARD + [str(parent.pid), "--", sys.executable, "-c",
                                      "import time; time.sleep(60)"],
                             stderr=subprocess.PIPE)
    time.sleep(0.5)
    parent.kill(); parent.wait()
    assert _wait_gone(guard, 8), "guard kept the server alive after its parent died"
    assert guard.returncode == 3
    assert b"parent" in guard.stderr.read()


def test_guard_mirrors_the_servers_own_exit_code():
    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        guard = subprocess.Popen(GUARD + [str(parent.pid), "--", sys.executable, "-c",
                                          "import sys; sys.exit(7)"])
        assert _wait_gone(guard, 8)
        assert guard.returncode == 7
    finally:
        parent.kill(); parent.wait()


def test_guard_forwards_sigterm_to_the_server():
    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        guard = subprocess.Popen(GUARD + [str(parent.pid), "--", sys.executable, "-c",
                                          "import time; time.sleep(30)"],
                                 start_new_session=True)
        time.sleep(0.5)
        os.killpg(guard.pid, signal.SIGTERM)        # how the adapters stop a tree
        assert _wait_gone(guard, 8), "SIGTERM to the group did not end the guard"
    finally:
        parent.kill(); parent.wait()
