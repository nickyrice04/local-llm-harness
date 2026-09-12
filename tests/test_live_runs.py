"""Detached runs: a run outlives the tab that started it, stops only on
the explicit cancel, and replays coalesced to a late consumer. Plus the
edit-diff the tool_result frame now carries for the chat's diff card."""

import asyncio
import json
import uuid

import pytest

from seymour import live_runs, runtime
from seymour.config import settings
from seymour.engine.adapter import GenerationRequest
from seymour.engine.fake import FakeEngine
from seymour.scheduler.core import Scheduler
from seymour.tools import files, paths


@pytest.fixture(autouse=True)
def clean_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.__class__, "workspace_dir",
                        property(lambda self: tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    files._seen.clear()
    yield


async def frames_of(*items: dict):
    """An async generator standing in for the executor."""
    for item in items:
        yield item
        await asyncio.sleep(0.001)


async def test_a_consumer_leaving_does_not_stop_the_run():
    seen: list[dict] = []
    gate = asyncio.Event()

    async def slow():
        yield {"run_id": "r1"}
        yield {"delta": "a"}
        await gate.wait()
        yield {"delta": "b"}
        seen.append({"finished": True})

    live = live_runs.start("s1", slow())
    follower = live_runs.follow(live)
    assert (await follower.__anext__()) == {"run_id": "r1"}
    await follower.aclose()                      # the tab closed
    assert live.queues == set()                  # …and unsubscribed itself
    gate.set()
    await live.task
    assert seen == [{"finished": True}]          # the run ran to the end regardless
    assert live.done and live.frames[-1] == {"done": True}


async def test_cancel_is_the_explicit_stop_and_replay_is_coalesced():
    gate = asyncio.Event()

    async def forever():
        yield {"run_id": "r2"}
        for ch in "hello":
            yield {"delta": ch}
        yield {"tool_progress": {"name": "write_file", "path": "a.html", "delta": "<p>", "tail": "<p>", "chars": 3, "seconds": 1}}
        yield {"tool_progress": {"name": "write_file", "path": "a.html", "delta": "hi", "tail": "<p>hi", "chars": 5, "seconds": 2}}
        await gate.wait()
        yield {"delta": "never"}

    live = live_runs.start("s2", forever())
    await asyncio.sleep(0.05)
    # Five one-character deltas replay as ONE; two progress frames fold into one carrying the whole content.
    deltas = [f for f in live.frames if "delta" in f and len(f) == 1]
    assert deltas == [{"delta": "hello"}]
    progress = [f for f in live.frames if "tool_progress" in f]
    assert len(progress) == 1 and progress[0]["tool_progress"]["delta"] == "<p>hi" and progress[0]["tool_progress"]["chars"] == 5
    assert live_runs.by_run("r2") is live and live_runs.by_session("s2") is live
    assert await live_runs.cancel("r2") is True
    assert live.done and {"error": "stopped"} in live.frames and live.frames[-1] == {"done": True}
    assert await live_runs.cancel("r2") is False   # a second stop is a stale click, not an error
    # A late consumer gets the replay and the end.
    late = [f async for f in live_runs.follow(live)]
    assert late[0] == {"run_id": "r2"} and late[-1] == {"done": True}


async def test_a_second_run_on_a_busy_session_is_refused_by_the_route(monkeypatch):
    from fastapi.testclient import TestClient
    from seymour.app import app
    from seymour.db import ChatSession, SessionLocal, init_db
    init_db()
    engine = FakeEngine(tokens=3, delay=0.0)
    caps = await engine.capabilities()
    monkeypatch.setattr(runtime, "scheduler", Scheduler(engine, caps))
    monkeypatch.setattr(runtime, "caps", caps)
    sid = str(uuid.uuid4())
    with SessionLocal() as db:
        db.add(ChatSession(id=sid, title="t")); db.commit()
    gate = asyncio.Event()

    async def held():
        yield {"run_id": "held"}
        await gate.wait()
    live_runs.start(sid, held())
    await asyncio.sleep(0.01)                    # let the drive task read the run_id frame
    with TestClient(app) as client:
        response = client.post("/api/chat", json={"session_id": sid, "message": "again"})
        assert response.status_code == 409
        assert client.post("/api/runs/nope/cancel").json()["cancelled"] is False
        assert client.get("/api/runs/nope/live").status_code == 404
        session = client.get(f"/api/sessions/{sid}").json()
        assert session["live_run_id"] == "held" and session["active"] is True
    gate.set()


async def test_the_tool_result_frame_carries_a_bounded_diff_for_edits(monkeypatch):
    from seymour import run_executor
    from seymour.db import init_db
    init_db()
    ws = paths.workspace()
    (ws / "a.py").write_text("x = 1\ny = 2\nz = 3\n")
    tag = files.compute_tag("x = 1\ny = 2\nz = 3\n")
    await files.read_file("a.py")               # the seen-lines ledger must know the file
    log = run_executor.RunLog(str(uuid.uuid4()), run_executor.CHAT_POLICY)
    result = await run_executor._execute(log, run_executor.catalog_for(run_executor.CHAT_POLICY),
                                         "edit_lines", {"path": "a.py", "tag": tag, "start": 2, "end": 2, "text": "y = 22"})
    frame = run_executor._tool_result_frame("edit_lines", {"path": "a.py"}, result, log)["tool_result"]
    assert frame["ok"] and frame["diff"]["added"] == 1 and frame["diff"]["removed"] == 1
    assert any(l == "-y = 2" for l in frame["diff"]["lines"]) and any(l == "+y = 22" for l in frame["diff"]["lines"])
    assert frame["diff"]["truncated"] is False and frame["chars"] == len(result)
    # A read carries no diff; the head is the result's first 2000 chars.
    result = await run_executor._execute(log, run_executor.catalog_for(run_executor.CHAT_POLICY), "read_file", {"path": "a.py"})
    frame = run_executor._tool_result_frame("read_file", {"path": "a.py"}, result, log)["tool_result"]
    assert frame["diff"] is None and frame["head"].startswith("[a.py#")
