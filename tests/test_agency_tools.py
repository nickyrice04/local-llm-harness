"""The agency primitives of 2026-09-11: todo_write, glob, read_structure,
background jobs, the persistent shell, git tools, read_image,
ask_user_question and the task subagent — each proven model-free (the
subagent on a scripted fake engine), with the sandbox where it applies."""

import asyncio
import socket
import struct
import subprocess
import sys
import uuid
import zlib

import httpx
import pytest

from seymour import runtime, tools
from seymour.config import settings
from seymour.engine.adapter import GenerationRequest
from seymour.engine.fake import FakeEngine
from seymour.scheduler.core import Scheduler
from seymour.tools import ask, context, files, git, images, jobs, paths, shell, structure, subagent, todo


@pytest.fixture(autouse=True)
def clean_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.__class__, "workspace_dir",
                        property(lambda self: tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    files._seen.clear()
    todo._plans.clear()
    yield


# --------------------------------------------------------------------------- #
#  todo_write                                                                 #
# --------------------------------------------------------------------------- #

async def test_todo_write_accepts_both_shapes_keeps_the_plan_per_run_and_forgets_it():
    tokens = context.scope("run-a")
    try:
        out = await tools.execute("todo_write", {"todos": [{"content": "read", "status": "done"},
                                                            {"content": "fix", "status": "in_progress"},
                                                            {"content": "test", "status": "later"}]})
        assert "[x] read" in out and "[>] fix" in out and "[ ] test" in out and "(1/3 done)" in out
        assert "not one of pending/in_progress/done" in out          # the bad status was named, not hidden
        assert [i["status"] for i in todo.current("run-a")] == ["done", "in_progress", "pending"]
        out = await tools.execute("todo_write", {"todos": "[x] read\n[x] fix\n[ ] test"})
        assert "(2/3 done)" in out
    finally:
        context.unscope(tokens)
    assert todo.current("run-b") == []                               # another run has its own plan
    todo.forget("run-a")
    assert todo.current("run-a") == []
    assert (await tools.execute("todo_write", {"todos": ""})).startswith("Error:")


def test_structured_args_travel_as_json_not_python_repr():
    coerced = tools._coerce(tools.TOOLS["todo_write"], {"todos": [{"content": "a", "status": "done"}]})
    assert coerced["todos"] == '[{"content": "a", "status": "done"}]'


# --------------------------------------------------------------------------- #
#  glob + read_structure                                                      #
# --------------------------------------------------------------------------- #

async def test_glob_finds_by_pattern_newest_first_and_read_structure_outlines_code():
    ws = paths.workspace()
    (ws / "src").mkdir()
    (ws / "src" / "old.py").write_text("x = 1\n")
    (ws / "src" / "app.py").write_text(
        "import os\nfrom sys import argv\n\nLIMIT = 3\n\nclass Thing:\n    def go(self):\n        return 1\n\n"
        "@decorate\nasync def main():\n    pass\n")
    (ws / "page.html").write_text("<div id=\"root\"></div>\n<script>\nfunction tick() {}\n</script>\n")
    (ws / "notes.md").write_text("# Title\n\ntext\n\n## Part\n")
    import os, time
    os.utime(ws / "src" / "app.py", (time.time() + 10, time.time() + 10))
    out = await tools.execute("glob", {"pattern": "**/*.py"})
    assert out.splitlines()[0].startswith("2 file(s) match")
    assert out.splitlines()[1] == "src/app.py"                       # newest first
    assert "No files match" in await tools.execute("glob", {"pattern": "*.rs"})
    shape = await tools.execute("read_structure", {"path": "src/app.py"})
    assert shape.startswith("[src/app.py#") and "structure" in shape.splitlines()[0]
    assert "6:class Thing:" in shape and "7:    def go(self):" in shape and "11:async def main():" in shape
    assert "4:LIMIT = 3" in shape and "10:@decorate" in shape and "return 1" not in shape   # bodies elided
    assert "7 declarations in 12 lines" in shape
    html = await tools.execute("read_structure", {"path": "page.html"})
    assert '1:<div id="root">' in html and "3:function tick() {}" in html
    md = await tools.execute("read_structure", {"path": "notes.md"})
    assert "1:# Title" in md and "5:## Part" in md
    (ws / "data.csv").write_text("a,b\n1,2\n")
    assert "no structure recognised" in await tools.execute("read_structure", {"path": "data.csv"})


# --------------------------------------------------------------------------- #
#  Background jobs                                                             #
# --------------------------------------------------------------------------- #

async def test_jobs_run_detached_read_consumingly_and_die_on_kill():
    py = sys.executable
    out = await tools.execute("run_in_background", {"command": f"{py} -c \"import time,sys; print('hello'); sys.stdout.flush(); time.sleep(30); print('never')\""})
    assert out.startswith("[job ") and "running" in out and "log: .seymour/artifacts/job-" in out
    job_id = out.split()[1]
    first = await tools.execute("job_output", {"job_id": job_id, "wait_s": 3})
    assert "hello" in first and "running" in first
    second = await tools.execute("job_output", {"job_id": job_id})
    assert "hello" not in second and "(no new output)" in second        # consuming reads
    killed = await tools.execute("job_kill", {"job_id": job_id})
    assert "job " in killed and ("killed" in killed or "exited" in killed)
    assert "already finished" in await tools.execute("job_kill", {"job_id": job_id})
    assert (await tools.execute("job_output", {"job_id": "nope"})).startswith("Error: no job")


@pytest.mark.skipif(not shell.SANDBOX_EXEC, reason="sandbox-exec is macOS only")
async def test_a_background_server_may_bind_localhost_inside_the_sandbox():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    out = await tools.execute("run_in_background", {"command": f"{sys.executable} -m http.server {port} --bind 127.0.0.1"})
    job_id = out.split()[1]
    try:
        ok = False
        async with httpx.AsyncClient() as client:
            for _ in range(40):
                try:
                    response = await client.get(f"http://127.0.0.1:{port}/", timeout=1.0)
                    ok = response.status_code == 200
                    break
                except httpx.HTTPError:
                    await asyncio.sleep(0.1)
        assert ok, "the sandboxed server never answered on localhost"
        # The one-shot run_command sandbox still has NO network at all.
        denied = await tools.execute("run_command", {"command": f"{sys.executable} -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:{port}/', timeout=2)\"", "timeout_s": 10})
        assert "exit code 0" not in denied
    finally:
        await tools.execute("job_kill", {"job_id": job_id})


# --------------------------------------------------------------------------- #
#  The persistent shell                                                        #
# --------------------------------------------------------------------------- #

async def test_shell_keeps_cwd_and_variables_and_a_timeout_ends_the_session():
    tokens = context.scope("run-shell")
    try:
        (paths.workspace() / "sub").mkdir()
        first = await tools.execute("shell", {"command": "cd sub && export MARK=42 && echo in-sub"})
        assert first.startswith("[new shell session") and "exit code 0" in first and "in-sub" in first
        second = await tools.execute("shell", {"command": "pwd; echo MARK=$MARK"})
        assert second.rstrip().endswith("MARK=42") and "/sub" in second and "new shell session" not in second
        failing = await tools.execute("shell", {"command": "exit 3"})   # exits the shell itself…
        assert "exit code 3" in failing or "killed" in failing
        after = await tools.execute("shell", {"command": "echo MARK=$MARK", "timeout_s": 5})
        assert "new shell session" in after and "MARK=\n" in after + "\n"   # …so the next call is fresh
        hung = await tools.execute("shell", {"command": "sleep 30", "timeout_s": 1})
        assert "TIMED OUT" in hung and "ended by the timeout" in hung
    finally:
        await shell.close_all_shells("run-shell")
        context.unscope(tokens)


# --------------------------------------------------------------------------- #
#  git                                                                        #
# --------------------------------------------------------------------------- #

async def test_git_tools_report_status_diffs_and_hunks():
    ws = paths.workspace()
    assert (await tools.execute("git_overview", {})).startswith("Error: the workspace is not a git repository")
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    (ws / "a.py").write_text("\n".join(f"line {i}" for i in range(1, 21)) + "\n")
    subprocess.run(["git", "add", "."], cwd=ws, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "first"], cwd=ws, check=True)
    lines = (ws / "a.py").read_text().splitlines()
    lines[2] = "line 3 changed"; lines[17] = "line 18 changed"
    (ws / "a.py").write_text("\n".join(lines) + "\n")
    (ws / "new.txt").write_text("hi\n")
    overview = await tools.execute("git_overview", {})
    assert "branch:" in overview and " M a.py" in overview and "?? new.txt" in overview and "first" in overview
    diff = await tools.execute("git_file_diff", {"path": "a.py"})
    assert "-line 3\n" in diff and "+line 3 changed" in diff
    assert "untracked" in await tools.execute("git_file_diff", {"path": "new.txt"})
    hunk = await tools.execute("git_hunk", {"path": "a.py", "start": 17, "end": 19})
    assert "line 18 changed" in hunk and "line 3 changed" not in hunk
    assert "no changes overlap" in await tools.execute("git_hunk", {"path": "a.py", "start": 9, "end": 11})


# --------------------------------------------------------------------------- #
#  read_image                                                                  #
# --------------------------------------------------------------------------- #

def _png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


async def test_read_image_is_honest_about_vision_and_attaches_when_it_can(monkeypatch):
    (paths.workspace() / "shot.png").write_bytes(_png(12, 7))
    monkeypatch.setattr(runtime, "caps", None)
    out = await tools.execute("read_image", {"path": "shot.png"})
    assert "12×7 px" in out and "NO vision" in out and context.take_attachments() == []
    caps = await FakeEngine().capabilities()
    monkeypatch.setattr(runtime, "caps", caps.__class__(**{**caps.__dict__, "supports_vision": True}))
    out = await tools.execute("read_image", {"path": "shot.png"})
    assert "attached below" in out
    parts = context.take_attachments()
    assert len(parts) == 1 and parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert context.take_attachments() == []                            # taken once
    assert (await tools.execute("read_image", {"path": "missing.png"})).startswith("Error:")


# --------------------------------------------------------------------------- #
#  ask_user_question                                                          #
# --------------------------------------------------------------------------- #

async def test_ask_user_question_uses_the_run_channel_or_says_nobody_is_listening():
    assert "nobody is watching" in await tools.execute("ask_user_question", {"question": "which?"})
    seen: list[dict] = []

    async def asker(q: dict) -> str:
        seen.append(q)
        return "the blue one"
    context.set_asker(asker)
    try:
        out = await tools.execute("ask_user_question", {"question": "which?", "options": "red | blue"})
        assert out == "Your person answered: the blue one" and seen[0]["options"] == ["red", "blue"]
        out = await tools.execute("ask_user_question", {"question": "again?", "options": '["a", "b"]'})
        assert seen[1]["options"] == ["a", "b"]
    finally:
        context.set_asker(None)
    assert (await tools.execute("ask_user_question", {"question": ""})).startswith("Error:")


async def test_the_executor_relays_a_question_card_and_the_answer_route_delivers(monkeypatch):
    """Through stream_chat_run: the tool asks, a {"question"} frame appears,
    answer() resolves it, and the tool result carries the answer."""
    from seymour import run_executor
    from seymour.db import ChatSession, SessionLocal, init_db
    init_db()

    class Scripted(FakeEngine):
        def __init__(self):
            super().__init__(delay=0.0, tokens=1)
            self.replies = ['{"tool": "ask_user_question", "args": {"question": "tea or coffee?", "options": ["tea", "coffee"]}}',
                            "You chose well."]

        async def _generate(self, req: GenerationRequest):
            self.served.append(req)
            reply = self.replies.pop(0) if self.replies else "done"
            yield reply
            req.stats.update({"prompt_tokens": 1, "generated_tokens": 1, "decode_tps": 1.0, "tps_source": "engine"})
    engine = Scripted()
    caps = await engine.capabilities()
    monkeypatch.setattr(runtime, "scheduler", Scheduler(engine, caps))
    monkeypatch.setattr(runtime, "caps", caps)
    sid = str(uuid.uuid4())
    with SessionLocal() as db:
        db.add(ChatSession(id=sid, title="t")); db.commit()
    base = [{"role": "system", "content": "sys"}, {"role": "user", "content": "drink?"}]
    frames: list[dict] = []

    async def drive():
        async for frame in run_executor.stream_chat_run(sid, base, "drink?", False, base[1:]):
            frames.append(frame)
            if "question" in frame:
                asyncio.get_running_loop().call_later(0.05, run_executor.answer, frame["question"]["run_id"], "tea")
    await asyncio.wait_for(drive(), 10)
    question = next(f["question"] for f in frames if "question" in f)
    assert question["question"] == "tea or coffee?" and question["options"] == ["tea", "coffee"]
    assert any(f.get("answered") for f in frames)
    # The model's second call saw the answer as the tool result.
    second = engine.served[1].messages[-1]["content"]
    assert "Your person answered: tea" in second
    assert run_executor.answer("nope", "x") is False


# --------------------------------------------------------------------------- #
#  task (subagent)                                                            #
# --------------------------------------------------------------------------- #

async def test_the_contract_shape_and_the_depth_guard():
    text = subagent.contract("app/", "add a docstring", "py_compile passes")
    assert text.startswith("# Target\napp/\n\n# Change\nadd a docstring\n\n# Acceptance\npy_compile passes")
    tokens = context.scope("child", 1)
    try:
        assert (await subagent.task("x", "y", "z")).startswith("Error: a subagent may not")
    finally:
        context.unscope(tokens)


async def test_a_task_runs_a_child_to_done_and_returns_a_summary_plus_the_artifact(monkeypatch):
    from seymour.agent import manager as manager_mod
    from seymour.agent.discrete import discrete
    from seymour.db import AgentStep, AgentTask, SessionLocal, init_db
    init_db()
    monkeypatch.setattr(manager_mod.settings, "agent_step_pause", 0.001)

    class Scripted(FakeEngine):
        def __init__(self):
            super().__init__(delay=0.0, tokens=1)

        async def _generate(self, req: GenerationRequest):
            self.served.append(req)
            goal = next((m["content"] for m in req.messages if str(m.get("content", "")).startswith("Your task:\n# Target")), "")
            yield "DONE: wrote the docstring; acceptance holds." if goal else "irrelevant"
            req.stats.update({"prompt_tokens": 1, "generated_tokens": 1, "decode_tps": 1.0, "tps_source": "engine"})
    engine = Scripted()
    caps = await engine.capabilities()
    monkeypatch.setattr(runtime, "scheduler", Scheduler(engine, caps))
    monkeypatch.setattr(runtime, "caps", caps)
    monkeypatch.setattr(subagent, "POLL_S", 0.05)
    out = await asyncio.wait_for(tools.execute("task", {"target": "app/x.py", "change": "add a docstring",
                                                          "acceptance": "py_compile passes"}), 15)
    assert out.startswith("[subagent ") and "· done ·" in out
    assert "wrote the docstring" in out and "[full journal: .seymour/artifacts/task-" in out
    artifact = out.split("[full journal: ")[1].split(" ")[0]
    text = (paths.workspace() / artifact).read_text()
    assert "# Target\napp/x.py" in text and "## Status: done" in text and "## Journal" in text
    # The child's prompt was the contract, with the tools, at depth 1 (no session message written).
    with SessionLocal() as db:
        task_row = db.query(AgentTask).filter(AgentTask.goal.startswith("# Target")).one()
        assert task_row.session_id == "" and task_row.status == "done"
    await discrete.shutdown()
