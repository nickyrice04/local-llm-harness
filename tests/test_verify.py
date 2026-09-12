"""auto_check: the harness verifies every file the model writes."""

import asyncio

import pytest

from seymour.config import settings
from seymour.tools import files, verify


@pytest.fixture(autouse=True)
def clean_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.__class__, "workspace_dir",
                        property(lambda self: tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    files._seen.clear()
    yield


def test_json_and_python_checks_pass_and_fail_honestly(tmp_path):
    ws = tmp_path / "ws"
    (ws / "ok.json").write_text('{"a": 1}')
    (ws / "bad.json").write_text('{"a": ')
    (ws / "ok.py").write_text("x = 1\n")
    (ws / "bad.py").write_text("def (:\n")
    assert asyncio.run(verify.auto_check("ok.json"))["verdict"] == "PASS"
    assert asyncio.run(verify.auto_check("bad.json"))["verdict"] == "FIX NEEDED"
    assert asyncio.run(verify.auto_check("ok.py"))["verdict"] == "PASS"
    bad = asyncio.run(verify.auto_check("bad.py"))
    assert bad["verdict"] == "FIX NEEDED" and "does not compile" in bad["report"]


def test_unchecked_kinds_and_missing_files_return_none(tmp_path):
    (tmp_path / "ws" / "notes.md").write_text("hi")
    assert asyncio.run(verify.auto_check("notes.md")) is None
    assert asyncio.run(verify.auto_check("gone.py")) is None
    assert asyncio.run(verify.auto_check("../x.py")) is None


def test_html_without_a_browser_is_not_measured_never_pass(tmp_path, monkeypatch):
    (tmp_path / "ws" / "p.html").write_text("<html></html>")
    # No tab open AND no headless browser (Playwright is installed on this
    # machine now, so the fallback is stubbed out): the only honest answer.
    from seymour.tools import probe

    async def no_headless(target, window):
        return None
    monkeypatch.setattr(probe, "_headless", no_headless)
    check = asyncio.run(verify.auto_check("p.html"))
    assert check["kind"] == "check_page" and check["verdict"] == "not measured"
    assert verify.summarize(check) == "check_page: not measured"
