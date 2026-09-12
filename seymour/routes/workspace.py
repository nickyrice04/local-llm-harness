"""Serve files from the agent's workspace to the browser — for the
document viewer to open what a run produced (an HTML app, a PDF, a
report) without a download step.

Confinement is the tools' own: `paths.resolve` refuses anything outside
the workspace or secret-shaped. HTML is served with a sandboxing CSP so
even a direct navigation runs it without network access or access to the
app's origin — the viewer additionally puts it in a sandboxed iframe.
"""

import mimetypes

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from seymour import tools
from seymour.tools import files, paths

router = APIRouter(prefix="/api/workspace")


_HTML_CSP = (
    # The produced page may run its own scripts (that is the point of an
    # HTML app) but reaches nothing: no network, no parent origin.
    "sandbox allow-scripts allow-pointer-lock allow-modals; "
    "default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval'; "
    "style-src 'unsafe-inline'; img-src data: blob:; font-src data:; "
    "media-src data: blob:")


@router.get("/file")
async def file(path: str, probe: str | None = None, seconds: int = 3):
    """One workspace file, by workspace-relative path. With `probe=<id>`
    an HTML page is served with check_page's reporting shim injected
    (see tools/probe.py) — only the hidden probe iframe asks for that."""
    try:
        target = paths.resolve(path)
    except ValueError as error:
        raise HTTPException(404, str(error))
    if not target.is_file():
        raise HTTPException(404, f"no such file: {path}")
    media, _ = mimetypes.guess_type(target.name)
    media = media or "application/octet-stream"
    headers = {}
    if media in ("text/html", "application/xhtml+xml"):
        headers["Content-Security-Policy"] = _HTML_CSP
        if probe:
            from seymour.tools import probe as probe_tool
            if probe_tool.pending_path(probe) != paths.display(target):
                # An id is issued for ONE file by a running check_page call;
                # anything else gets the plain page, no shim.
                return FileResponse(target, media_type=media, headers=headers)
            html = target.read_text(encoding="utf-8", errors="replace")
            # Stricter for a probe: no modals (alert() would block the
            # observation) and no pointer lock; scripts still run.
            headers["Content-Security-Policy"] = _HTML_CSP.replace(
                " allow-pointer-lock allow-modals", "")
            return HTMLResponse(probe_tool.inject_shim(html, probe, max(1, min(seconds, probe_tool.MAX_SECONDS))),
                                headers=headers)
    return FileResponse(target, media_type=media, headers=headers)


@router.post("/probe/{probe_id}")
async def probe_report(probe_id: str, report: dict):
    """The open tab reports what a probed page did; the waiting
    check_page call receives it. Untrusted text — the tool wraps it."""
    from seymour.tools import probe as probe_tool
    return {"delivered": probe_tool.resolve(probe_id, report)}


# --------------------------------------------------------------------------- #
#  The Code workspace: what the editor view reads, writes and runs.           #
# --------------------------------------------------------------------------- #
#
# Why these exist (2026-09-03): Nick watched a page being generated as a
# bare "…" and had "no idea what was being generated … no dedicated code
# editor where Seymour can test and edit code". The Code view needs the
# same four things the model's tools have — list, read, write, run — over
# the same confined workspace, with one extra rule: a person's save and
# the model's edit must never silently clobber each other. The content
# TAG (files.compute_tag) is that rule: a write may carry the tag it
# last saw and is refused with 409 when the file moved on. The model's
# edit_lines already refuses on a stale tag, so a human save simply makes
# the model re-read — no shared lock, no lost work either way.

MAX_TREE_ENTRIES = 2000


@router.get("/tree")
async def tree():
    """Every file under the workspace as a flat, sorted list of
    {path, size, kind} — the view builds the folders. Noise dirs
    (paths.SKIP_DIRS) and secret-shaped names are left out, exactly as
    the model's list_files leaves them out."""
    root = paths.workspace()
    out = []
    for entry in files._walk(root):
        if paths.is_sensitive(entry):
            continue
        rel = str(entry.relative_to(root))
        try:
            size = entry.stat().st_size
        except OSError:
            continue
        suffix = entry.suffix.lower()
        kind = ("html" if suffix in (".html", ".htm") else
                "code" if suffix in (".py", ".js", ".ts", ".css", ".json", ".sh", ".toml", ".yaml", ".yml") else
                "text" if suffix in (".md", ".txt", ".csv", ".tsv", "") else
                "doc" if suffix in (".pdf", ".docx", ".pptx", ".xlsx") else "other")
        out.append({"path": rel, "size": size, "kind": kind})
        if len(out) >= MAX_TREE_ENTRIES:
            break
    return {"root": str(root), "files": out, "truncated": len(out) >= MAX_TREE_ENTRIES}


@router.get("/read")
async def read(path: str):
    """A text file with its content tag — the same tag the model sees in
    read_file's [path#TAG] header, so the two views agree on 'current'."""
    try:
        target = paths.resolve(path)
        text = files._read_text(target)
    except ValueError as error:
        raise HTTPException(404 if "no such" in str(error) else 400, str(error))
    return {"path": paths.display(target), "content": text,
            "tag": files.compute_tag(text), "lines": text.count("\n") + (0 if text.endswith("\n") or not text else 1)}


class WriteBody(BaseModel):
    path: str
    content: str
    expected_tag: str | None = None       # the tag the editor loaded; None = no check


@router.post("/write")
async def write(body: WriteBody):
    """Save from the editor. With expected_tag, the save is refused (409)
    when the file no longer carries that tag — someone (the model, most
    likely) changed it meanwhile; the reply carries the current content
    so the person can merge instead of overwrite."""
    try:
        target = paths.resolve(body.path)
    except ValueError as error:
        raise HTTPException(400, str(error))
    if target.exists() and target.is_dir():
        raise HTTPException(400, f"{body.path} is a directory")
    current = files._read_text(target) if target.exists() else ""
    current_tag = files.compute_tag(current) if target.exists() else None
    if body.expected_tag is not None and current_tag is not None and body.expected_tag != current_tag:
        raise HTTPException(409, {"message": "the file changed since you opened it",
                                  "current_tag": current_tag, "current": current})
    files._write_text(target, body.content)
    return {"path": paths.display(target), "tag": files.compute_tag(body.content),
            "bytes": len(body.content.encode("utf-8"))}


class RunBody(BaseModel):
    command: str
    timeout_s: int | None = None


@router.post("/run")
async def run(body: RunBody):
    """Run one command exactly as the model's run_command would: same
    sandbox, same workspace, same result text (exit code, head + tail,
    spill file). The person and the model see the same thing."""
    args = {"command": body.command}
    if body.timeout_s:
        args["timeout_s"] = body.timeout_s
    result = await tools.execute("run_command", args)
    return {"result": result}


class RunToolBody(BaseModel):
    tool: str
    args: dict = {}


@router.post("/run-tool")
async def run_tool(body: RunToolBody):
    """The Code pane invoking one of the model's READ-tier tools (check_page,
    read_file, grep…) or run_command, exactly as the model would — same
    registry, same result text. Write-tier tools are the editor's own
    Save; exec beyond run_command is not offered here."""
    tool = tools.TOOLS.get(body.tool)
    if tool is None or (tool.tier != "read" and body.tool != "run_command"):
        raise HTTPException(400, f"{body.tool} is not a tool the Code pane may run")
    return {"result": await tools.execute(body.tool, body.args)}
