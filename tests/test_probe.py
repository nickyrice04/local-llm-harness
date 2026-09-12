"""check_page: the browser-side runtime check for generated pages."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from seymour import tools
from seymour.config import settings
from seymour.tools import files, probe


@pytest.fixture(autouse=True)
def clean_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.__class__, "workspace_dir",
                        property(lambda self: tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    files._seen.clear()
    yield


def test_shim_goes_first_in_head_or_at_the_top_without_moving_lines():
    page = "<!DOCTYPE html>\n<html>\n<head>\n<title>x</title>\n</head>\n<body></body>\n</html>"
    out = probe.inject_shim(page, "abc", 3)
    assert out.index("data-seymour-probe") < out.index("<title>")
    assert '"abc"' in out and "S=3" in out
    assert out.count("\n") == page.count("\n"), "the shim must keep the page's line numbers"
    bare = "<p>no head</p>"
    assert probe.inject_shim(bare, "id1", 2).startswith("<script data-seymour-probe>")


def test_check_page_without_a_browser_says_so_and_rejects_non_html(tmp_path):
    (tmp_path / "ws" / "page.html").write_text("<html></html>")
    (tmp_path / "ws" / "notes.md").write_text("x")
    out = asyncio.run(tools.execute("check_page", {"path": "page.html"}))
    # No /api/events subscriber in tests and no Playwright → the honest answer.
    assert "no Seymour tab is open" in out or "Playwright" in out
    assert "check_page is for .html" in asyncio.run(tools.execute("check_page", {"path": "notes.md"}))
    assert "no such file" in asyncio.run(tools.execute("check_page", {"path": "gone.html"}))


def test_probe_route_serves_the_shim_and_reports_resolve_the_waiting_tool(tmp_path):
    from seymour.app import app
    client = TestClient(app)
    (tmp_path / "ws" / "page.html").write_text("<html><head></head><body><script>x()</script></body></html>")
    # Only a pending id issued for THIS file gets the shim.
    assert "data-seymour-probe" not in client.get("/api/workspace/file", params={"path": "page.html", "probe": "p1"}).text
    probe._pending_paths["p1"] = "page.html"
    served = client.get("/api/workspace/file", params={"path": "page.html", "probe": "p1", "seconds": 2}).text
    assert "data-seymour-probe" in served and "S=2" in served
    probe._pending_paths.pop("p1", None)
    assert "sandbox allow-scripts" in client.get("/api/workspace/file", params={"path": "page.html"}).headers["content-security-policy"]

    async def wait_and_report():
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        probe._pending["p2"] = fut
        assert probe.resolve("p2", {"errors": ["x is not defined (line 1)"], "elements": 4,
                                    "canvases": 0, "ids": 0, "rafCalls": 0, "title": "t",
                                    "missingIds": ["stage"], "elapsed": 2100}) is True
        return await fut
    report = asyncio.run(wait_and_report())
    text = probe._format("page.html", report)
    assert "console errors: 1" in text and "x is not defined" in text
    assert "MISSING from the page: stage" in text and "verdict: FIX NEEDED" in text
    assert probe.resolve("nobody-waits", {}) is False
    assert client.post("/api/workspace/probe/nobody-waits", json={"errors": []}).json() == {"delivered": False}


def test_format_passes_a_clean_animated_page():
    text = probe._format("solar.html", {"errors": [], "elements": 40, "canvases": 1, "ids": 5,
                                        "rafCalls": 180, "title": "Solar", "missingIds": [],
                                        "elapsed": 3000, "bodyText": "Pause Speed"})
    assert "verdict: PASS" in text and "animation running" in text


def test_format_fails_an_empty_page():
    text = probe._format("empty.html", {"errors": [], "elements": 4, "canvases": 0, "ids": 0,
                                        "rafCalls": 0, "title": "", "missingIds": [], "elapsed": 3000, "bodyText": ""})
    assert "the page is empty" in text and "verdict: FIX NEEDED" in text
