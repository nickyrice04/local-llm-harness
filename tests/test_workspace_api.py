"""The Code workspace API: tree, read, tag-checked write, run — over the
same confined workspace the model's tools use."""

import pytest
from fastapi.testclient import TestClient

from seymour.config import settings
from seymour.tools import files


@pytest.fixture(autouse=True)
def clean_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.__class__, "workspace_dir",
                        property(lambda self: tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    files._seen.clear()
    yield


@pytest.fixture
def client():
    from seymour.app import app
    return TestClient(app)


def test_tree_lists_files_with_kinds_and_hides_secrets(client, tmp_path):
    ws = tmp_path / "ws"
    (ws / "page.html").write_text("<html></html>")
    (ws / "notes.md").write_text("hi")
    (ws / ".env").write_text("SECRET=1")
    (ws / "sub").mkdir(); (ws / "sub" / "a.py").write_text("print(1)")
    body = client.get("/api/workspace/tree").json()
    paths_seen = {f["path"]: f["kind"] for f in body["files"]}
    assert paths_seen == {"page.html": "html", "notes.md": "text", "sub/a.py": "code"}


def test_read_returns_the_same_tag_the_model_sees(client, tmp_path):
    (tmp_path / "ws" / "a.txt").write_text("one\ntwo\n")
    body = client.get("/api/workspace/read", params={"path": "a.txt"}).json()
    assert body["content"] == "one\ntwo\n" and body["lines"] == 2
    assert body["tag"] == files.compute_tag("one\ntwo\n")
    assert client.get("/api/workspace/read", params={"path": "missing.txt"}).status_code == 404
    assert client.get("/api/workspace/read", params={"path": "../etc/passwd"}).status_code == 400


def test_write_refuses_a_stale_tag_and_saves_a_fresh_one(client, tmp_path):
    target = tmp_path / "ws" / "a.txt"
    target.write_text("v1\n")
    stale = files.compute_tag("v0\n")
    refused = client.post("/api/workspace/write", json={"path": "a.txt", "content": "mine\n", "expected_tag": stale})
    assert refused.status_code == 409 and refused.json()["detail"]["current"] == "v1\n"
    ok = client.post("/api/workspace/write", json={"path": "a.txt", "content": "mine\n",
                                                   "expected_tag": files.compute_tag("v1\n")})
    assert ok.status_code == 200 and target.read_text() == "mine\n"
    assert ok.json()["tag"] == files.compute_tag("mine\n")
    # A brand-new file needs no tag; paths outside the workspace are refused.
    assert client.post("/api/workspace/write", json={"path": "new/b.txt", "content": "x"}).status_code == 200
    assert client.post("/api/workspace/write", json={"path": "../b.txt", "content": "x"}).status_code == 400


def test_run_goes_through_the_models_own_run_command(client):
    body = client.post("/api/workspace/run", json={"command": "echo hello-from-the-editor"}).json()
    assert "hello-from-the-editor" in body["result"] and "exit code 0" in body["result"]


def test_run_tool_allows_read_tools_and_run_command_only(client, tmp_path):
    (tmp_path / "ws" / "a.txt").write_text("alpha\n")
    body = client.post("/api/workspace/run-tool", json={"tool": "read_file", "args": {"path": "a.txt"}}).json()
    assert "1:alpha" in body["result"]
    assert client.post("/api/workspace/run-tool", json={"tool": "write_file", "args": {"path": "b", "content": "x"}}).status_code == 400
    assert client.post("/api/workspace/run-tool", json={"tool": "nope", "args": {}}).status_code == 400
