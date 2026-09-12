"""Skills: discovery across the three roots, precedence, the index the
prompt carries, enable/disable, create, and the use_skill tool."""

import asyncio
import json

import pytest

from seymour import skills, tools
from seymour.config import settings
from seymour.tools import paths


@pytest.fixture(autouse=True)
def roots(tmp_path, monkeypatch):
    """Fresh bundled/user/workspace roots for every test."""
    bundled = tmp_path / "bundled"; user = tmp_path / "data" / "skills"; ws = tmp_path / "ws"
    for d in (bundled, user, ws / ".seymour" / "skills"):
        d.mkdir(parents=True)
    monkeypatch.setattr(skills, "BUNDLED_DIR", bundled)
    # data_dir is a plain settings field; workspace_dir is a derived property.
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings.__class__, "workspace_dir", property(lambda self: ws))
    state = {}
    monkeypatch.setattr(skills, "get_state", lambda k, d=None: state.get(k, d))
    monkeypatch.setattr(skills, "set_state", lambda k, v: state.__setitem__(k, v))
    yield {"bundled": bundled, "user": user, "ws": ws / ".seymour" / "skills"}


def _write(root, name, description, body="Do the thing.", extra=""):
    d = root / name; d.mkdir(exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n{extra}---\n\n{body}\n")
    return d


def test_discovery_precedence_and_index(roots):
    _write(roots["bundled"], "spreadsheets", "Edit xlsx/csv with openpyxl", "bundled body")
    _write(roots["bundled"], "html-page", "Single-file pages", "x" * 10)
    _write(roots["user"], "spreadsheets", "My own spreadsheet rules", "user body",
           extra="allowed-tools: run_command, read_file\nversion: 2\n")
    d = _write(roots["ws"], "repo-conventions", "How this repo likes its code")
    (d / "checklist.md").write_text("- lint\n")
    found = skills.refresh()
    assert set(found) == {"spreadsheets", "html-page", "repo-conventions"}
    assert found["spreadsheets"].source == "user" and found["spreadsheets"].body == "user body"
    assert found["spreadsheets"].tools == ["run_command", "read_file"] and found["spreadsheets"].version == "2"
    index = skills.index_text()
    assert "- spreadsheets — My own spreadsheet rules" in index and "- html-page —" in index
    assert "bundled body" not in index                    # bodies never ride in the prompt
    # The workspace skill is rendered with an untrusted note and its files.
    text = skills.render("repo-conventions")
    assert "comes from the workspace" in text and "checklist.md" in text


def test_enable_disable_and_create(roots):
    _write(roots["bundled"], "html-page", "Single-file pages")
    skills.set_enabled("html-page", False)
    assert skills.index_text() == "" and skills.render("html-page").startswith("Error: the skill html-page is disabled")
    skills.set_enabled("html-page", True)
    assert "html-page" in skills.index_text()
    made = skills.create("my-notes", "  How I like   notes ", "# Steps\n1. write")
    assert made.source == "user" and made.description == "How I like notes"
    assert (roots["user"] / "my-notes" / "SKILL.md").read_text().startswith("---\nname: my-notes")
    with pytest.raises(FileExistsError):
        skills.create("my-notes", "again", "x")
    with pytest.raises(ValueError):
        skills.create("Bad Name!", "x", "y")
    with pytest.raises(KeyError):
        skills.set_enabled("nope", True)


def test_bad_front_matter_and_names_are_skipped_not_fatal(roots):
    d = roots["bundled"] / "Weird_Name"; d.mkdir()
    (d / "SKILL.md").write_text("---\nname: Weird_Name\ndescription: nope\n---\nbody")
    e = roots["bundled"] / "plain"; e.mkdir()
    (e / "SKILL.md").write_text("# Plain\n\nNo front matter, first line is the description.\n")
    found = skills.refresh()
    assert "weird_name" not in found and found["plain"].description.startswith("No front matter")


def test_use_skill_tool_and_catalog_index(roots):
    _write(roots["bundled"], "presentations", "Build pptx decks with python-pptx", "Open python-pptx…")
    catalog = tools.render_catalog("full")
    assert "Skills (load one with use_skill" in catalog and "- presentations — Build pptx" in catalog
    out = asyncio.run(tools.execute("use_skill", {"name": "presentations"}))
    assert out.startswith("[skill presentations] (bundled)") and "Open python-pptx" in out
    assert asyncio.run(tools.execute("use_skill", {"name": "missing"})).startswith("Error: no skill named")
    # Read tier: chat never gates it.
    assert tools.TOOLS["use_skill"].tier == "read"
