"""check_page: load a generated HTML page in a real browser and report
what happened — the check static reads cannot do.

Measured 2026-09-03: a solar-system page passed nine static checks and
threw `togglePause is not defined` at load; nothing in the harness could
have known. The Seymour UI IS a browser, so the tool asks it: the server
publishes a probe event, the open tab loads the page in a hidden
sandboxed iframe with a small reporting shim injected at the top of the
document, the shim posts errors and facts to the parent, the tab posts
them back to the server, and the tool returns them as text the model can
act on. No install, no network, the person's own browser. When no tab is
open, the answer is that — never a silent pass — and a headless
Playwright fallback is used when it is importable.
"""

import asyncio
import json
import secrets
from pathlib import Path

from seymour.events import bus
from seymour.verify import web as _web
from seymour.tools import Tool, paths

# id → the future the tool is awaiting; resolved by POST /api/workspace/probe/<id>.
_pending: dict[str, asyncio.Future] = {}

# How long a page gets to load and run before the shim reports (default),
# and the ceiling a caller may ask for.
DEFAULT_SECONDS = 3
MAX_SECONDS = 15

# The shim, injected as the FIRST script so it sees every later error.
# Plain ES5-ish, wrapped in try/catch, no dependencies: it must never
# change how the page behaves. It reports twice — once at `load`, once
# after the observation window — so a page that dies at load still
# yields a report.
SHIM = """<script data-seymour-probe>
(function(){try{
var ID=%(id)s,S=%(seconds)d,errors=[],warns=[],raf=0,intervals=0,t0=Date.now();
function post(kind){try{
  var d=document,ids=[],els=d.querySelectorAll('[id]');for(var i=0;i<els.length&&i<400;i++)ids.push(els[i].id);
  var missing=[],src='';try{var sc=d.querySelectorAll('script:not([data-seymour-probe])');for(var j=0;j<sc.length;j++)src+=sc[j].textContent+'\\n';}catch(e){}
  var re=/getElementById\\((['"])([^'"]+)\\1\\)|querySelector\\((['"])#([A-Za-z0-9_-]+)\\3\\)/g,m,seen={};
  while((m=re.exec(src))){var want=m[2]||m[4];if(want&&!seen[want]){seen[want]=1;if(!d.getElementById(want))missing.push(want);}}
  var cov=[];try{var cs=d.getElementsByTagName('canvas');for(var k=0;k<cs.length&&k<4;k++){var c=cs[k],w=c.width,h=c.height;if(!w||!h){cov.push(0);continue;}
    var s=d.createElement('canvas');s.width=24;s.height=24;var x=s.getContext('2d');x.drawImage(c,0,0,24,24);var px=x.getImageData(0,0,24,24).data,n=0,first=[px[0],px[1],px[2],px[3]];
    for(var q=0;q<px.length;q+=4){if(Math.abs(px[q]-first[0])+Math.abs(px[q+1]-first[1])+Math.abs(px[q+2]-first[2])>30||Math.abs(px[q+3]-first[3])>30)n++;}
    cov.push(Math.round(100*n/(px.length/4)));}}catch(e){cov=['unreadable'];}
  parent.postMessage({seymourProbe:ID,kind:kind,elapsed:Date.now()-t0,title:d.title,visibility:d.visibilityState,canvasCoverage:cov,
    elements:d.getElementsByTagName('*').length,canvases:d.getElementsByTagName('canvas').length,
    ids:ids.length,missingIds:missing.slice(0,20),rafCalls:raf,intervals:intervals,
    bodyText:(d.body&&d.body.innerText||'').replace(/\\s+/g,' ').trim().slice(0,160),
    errors:errors.slice(0,10),warnings:warns.slice(0,5)},'*');}catch(e){}}
window.addEventListener('error',function(e){errors.push((e.message||'error')+(e.filename?'':'')+(e.lineno?' (line '+e.lineno+')':''));});
window.addEventListener('unhandledrejection',function(e){errors.push('unhandled rejection: '+String(e.reason&&e.reason.message||e.reason));});
var ce=console.error,cw=console.warn;console.error=function(){errors.push('console.error: '+Array.prototype.slice.call(arguments).join(' ').slice(0,200));return ce&&ce.apply(console,arguments);};
console.warn=function(){warns.push(Array.prototype.slice.call(arguments).join(' ').slice(0,160));return cw&&cw.apply(console,arguments);};
var raf0=window.requestAnimationFrame;window.requestAnimationFrame=function(f){raf++;return raf0.call(window,f);};
var si0=window.setInterval;window.setInterval=function(){intervals++;return si0.apply(window,arguments);};
window.addEventListener('load',function(){post('load');setTimeout(function(){post('done');},S*1000);});
}catch(e){}})();
</script>"""


def inject_shim(html: str, probe_id: str, seconds: int) -> str:
    """The shim, collapsed to ONE line and inserted on the same line as the
    opening <head> (else <html>, else the doctype, else at the top) —
    so it runs before any page script AND every line number the page's
    own errors report stays exactly what the file says (verified: an
    error at line 28 reads 28 with and without the shim)."""
    shim = " ".join(line.strip() for line in (SHIM % {"id": json.dumps(probe_id), "seconds": seconds}).splitlines())
    lower = html.lower()
    for opener in ("<head", "<html", "<!doctype"):
        at = lower.find(opener)
        if at != -1:
            close = lower.find(">", at)
            if close != -1:
                return html[:close + 1] + shim + html[close + 1:]
    return shim + html


# id → the workspace-relative path it was issued for: the file route
# injects only into THAT file, so an id cannot be used to probe another.
_pending_paths: dict[str, str] = {}


def pending_path(probe_id: str) -> str | None:
    return _pending_paths.get(probe_id)


def resolve(probe_id: str, report: dict) -> bool:
    """The UI posted a report: hand it to the waiting tool (True if one was)."""
    fut = _pending.get(probe_id)
    if fut is None or fut.done():
        return False
    fut.set_result(report)
    return True


def _format(rel: str, report: dict) -> str:
    """A compact, model-readable verdict."""
    errors = report.get("errors") or []
    lines = [f"[check_page {rel}] loaded in the browser; observed {report.get('elapsed', 0) / 1000:.1f}s."]
    lines.append(f"console errors: {len(errors)}" + ("" if not errors else " — " + " | ".join(str(e)[:160] for e in errors[:5])))
    facts = [f"title {report.get('title')!r}", f"{report.get('elements', 0)} elements",
             f"{report.get('canvases', 0)} canvas", f"{report.get('ids', 0)} ids"]
    raf = report.get("rafCalls", 0)
    if raf == 0 and report.get("visibility") == "hidden":
        facts.append("animation not measured (the browser tab was hidden — browsers pause "
                     "requestAnimationFrame in hidden tabs; bring Seymour to the front and retry)")
    elif raf == -1:
        facts.append("animation not measured (headless)")
    else:
        facts.append(f"requestAnimationFrame calls: {raf}" + (" (animation running)" if raf > 10 else " (no animation loop)" if raf == 0 else ""))
    if report.get("intervals"):
        facts.append(f"setInterval timers: {report['intervals']}")
    coverage = report.get("canvasCoverage")
    if isinstance(coverage, list) and coverage:
        # What was actually DRAWN: the share of a canvas's pixels that differ
        # from its top-left pixel. 0% on an animated page means a blank canvas.
        facts.append("canvas pixels drawn: " + ", ".join(f"{c}%" if isinstance(c, int) else str(c) for c in coverage))
    lines.append("facts: " + "; ".join(facts) + ".")
    missing = report.get("missingIds") or []
    if missing:
        lines.append("ids referenced in scripts but MISSING from the page: " + ", ".join(missing))
    if report.get("warnings"):
        lines.append("console warnings: " + " | ".join(str(w)[:120] for w in report["warnings"][:3]))
    text = report.get("bodyText") or ""
    lines.append(f"visible text: {text[:120]!r}" if text else "visible text: (none — a blank page?)")
    blank_canvas = (isinstance(coverage, list) and coverage and all(c == 0 for c in coverage if isinstance(c, int))
                    and report.get("visibility") != "hidden")
    if blank_canvas:
        lines.append("the canvas is blank: nothing was drawn on it in the observation window")
    # An EMPTY page has no errors either. Measured 2026-09-03: a 0-byte
    # file passed as "0 console errors" — 4 elements is html+head+body+shim.
    empty_page = int(report.get("elements") or 0) <= 5 and not (report.get("bodyText") or "").strip() \
        and not report.get("canvases")
    if empty_page:
        lines.append("the page is empty: no content, no text, no canvas — nothing to check")
    verdict = "PASS" if not errors and not missing and not blank_canvas and not empty_page else "FIX NEEDED"
    lines.append(f"verdict: {verdict}")
    return "\n".join(lines)


async def _headless(target: Path, seconds: int) -> dict | None:
    """Playwright fallback (when installed): the same facts, no UI needed."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None
    errors: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append("console.error: " + m.text) if m.type == "error" else None)
        await page.goto(target.as_uri(), wait_until="load")
        await page.wait_for_timeout(seconds * 1000)
        facts = await page.evaluate("""() => ({title: document.title, elements: document.getElementsByTagName('*').length,
            canvases: document.getElementsByTagName('canvas').length, ids: document.querySelectorAll('[id]').length,
            bodyText: (document.body && document.body.innerText || '').replace(/\\s+/g,' ').trim().slice(0,160)})""")
        await browser.close()
    return {**facts, "errors": errors, "elapsed": seconds * 1000, "rafCalls": -1}


async def check_page(path: str, seconds: str | int = "", interact: str = "") -> str:
    """Tool entry: load a workspace HTML page in a browser and report
    console errors, missing ids, animation activity and visible text —
    and, with `interact`, DRIVE it: click / type / drag the things the
    task named and report whether the DOM changed (seymour.verify.web)."""
    try:
        target = paths.resolve(path)
    except ValueError as error:
        return f"Error: {error}"
    if not target.is_file():
        return f"Error: no such file: {path}"
    if target.suffix.lower() not in (".html", ".htm"):
        return f"Error: check_page is for .html pages; {path} is not one."
    report = await _check_page_load(path, target, seconds)
    steps = _web.parse_steps(interact) if interact else []
    if not steps:
        return report
    interaction = await _web.run_interactions(target, steps)
    if interaction is None:
        return report + "\n\n[interaction not measured: Playwright is not installed]"
    verdict = "PASS" if interaction.ok and not interaction.console_errors else "FIX NEEDED"
    text = interaction.text()
    if verdict == "FIX NEEDED" and "verdict: PASS" in report:
        # The load was clean but the page does not WORK: the interaction
        # verdict overrides, so the repair guard sends the model back.
        report = report.replace("verdict: PASS", "verdict: FIX NEEDED (interaction)")
    return report + f"\n\n[interaction] verdict: {verdict}\n{text}"


async def _check_page_load(path: str, target, seconds: str | int = "") -> str:
    """The load-time check (the original check_page body)."""
    try:
        window = max(1, min(int(str(seconds).strip() or DEFAULT_SECONDS), MAX_SECONDS))
    except ValueError:
        window = DEFAULT_SECONDS
    rel = paths.display(target)
    if bus.subscribers() == 0:
        report = await _headless(target, window)
        if report is None:
            return (f"check_page could not run: no Seymour tab is open in a browser and Playwright "
                    f"is not installed. Ask your person to open Seymour in a browser (the page is "
                    f"loaded there, sandboxed), then call check_page again.")
        return _format(rel, report).replace("loaded in the browser", "loaded headless (Playwright)")
    probe_id = secrets.token_urlsafe(12)
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _pending[probe_id] = fut
    _pending_paths[probe_id] = rel
    try:
        bus.publish("workspace", "probe", id=probe_id, path=rel, seconds=window,
                    url=f"/api/workspace/file?path={rel}&probe={probe_id}")
        report = await asyncio.wait_for(fut, timeout=window + 12)
    except asyncio.TimeoutError:
        return (f"check_page timed out: the open Seymour tab did not report within {window + 12}s "
                f"(is the tab in the background? browsers pause hidden tabs). Try again with the "
                f"tab visible, or after a shorter observation window.")
    finally:
        _pending.pop(probe_id, None)
        _pending_paths.pop(probe_id, None)
    if not isinstance(report, dict):
        return "check_page: malformed report from the browser."
    return _format(rel, report)


TOOLS = [
    Tool(
        name="check_page",
        description=("Load an .html file from the workspace in the person's browser (sandboxed, "
                     "hidden) and report what really happened: console errors with line numbers, "
                     "ids referenced by scripts but missing from the page, whether an animation "
                     "loop runs, element/canvas counts and visible text. Run it after writing a "
                     "page and fix everything it lists before finishing."),
        args={"path": "workspace-relative path of the .html file",
              "seconds": f"how long to let the page run before reporting (default {DEFAULT_SECONDS}, max {MAX_SECONDS})",
              "interact": ("optional steps to DRIVE the page, one per line: click <selector> · type <selector> <text> · "
                           "press <key> · drag <selector> <dx> <dy> · wait <ms> · expect <selector> visible|count <n>|text <substring> "
                           "· expect changed. Each action reports whether the DOM changed.")},
        optional=frozenset({"seconds", "interact"}),
        tier="read", func=check_page,
    ),
]
