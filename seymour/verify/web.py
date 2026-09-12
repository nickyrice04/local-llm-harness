"""Page interaction through Playwright: does the button WORK?

check_page (tools/probe.py) measures console errors, ids, animation
frames and canvas coverage — whether a page loads and runs. This module
adds the other half: a small script of interactions, each followed by
the question "did the DOM change?", plus screenshots at chosen moments.
Both the check_page tool (its `interact` argument) and the eval renderer
(evals/render/html.py) run through here, so the model's own check and
the judge's images come from the same code.

The step language, one step per line (or `;`-separated):

    click <selector>                    click the first match
    dblclick <selector>
    type <selector> <text…>             focus, then type the text
    press <key>                         a key on the focused element (Enter, Escape…)
    drag <selector> <dx> <dy>           press on its centre, move by (dx, dy), release
    wait <ms>
    expect <selector> visible           assert it exists and is visible
    expect <selector> text <substring>  assert its textContent contains the substring
    expect <selector> count <n>         assert exactly n matches
    expect changed                      assert the DOM changed since the last step
    shot <name>                         save a screenshot (when a shots dir is given)

A DOM change is a change in the page's serialized outerHTML (style
attributes included, so a moved window or a raised z-index counts).
"""

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

VIEWPORTS = {"desktop": (1440, 900), "laptop": (1024, 768), "phone": (375, 812)}
STEP_TIMEOUT_MS = 4000


@dataclass
class StepResult:
    step: str
    ok: bool
    note: str = ""
    changed: bool | None = None


@dataclass
class InteractionReport:
    ok: bool
    steps: list[StepResult] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    error: str = ""

    def text(self) -> str:
        lines = []
        for s in self.steps:
            mark = "✔" if s.ok else "✘"
            change = "" if s.changed is None else (" · DOM changed" if s.changed else " · DOM unchanged")
            lines.append(f"{mark} {s.step}{change}{(' — ' + s.note) if s.note else ''}")
        if self.console_errors:
            lines.append(f"console errors during interaction ({len(self.console_errors)}): " + " | ".join(e[:160] for e in self.console_errors[:5]))
        if self.error:
            lines.append(f"interaction run failed: {self.error}")
        return "\n".join(lines)


def parse_steps(script: str) -> list[str]:
    """Steps from text: lines, or `;`-separated on one line."""
    raw = [part.strip() for line in (script or "").splitlines() for part in line.split(";")]
    return [s for s in raw if s and not s.startswith("#")]


async def _dom_hash(page) -> str:
    html = await page.evaluate("() => document.documentElement.outerHTML")
    return hashlib.sha1(html.encode("utf-8", errors="replace")).hexdigest()


async def run_interactions(html_path: Path, steps: list[str], shots_dir: Path | None = None,
                           viewport: tuple[int, int] = VIEWPORTS["desktop"], settle_ms: int = 800,
                           url: str | None = None) -> InteractionReport | None:
    """Drive the page through the steps. None when Playwright (or its
    browser) is unavailable — the caller says "not measured"."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None
    report = InteractionReport(ok=True)
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport={"width": viewport[0], "height": viewport[1]})
            page.on("pageerror", lambda e: report.console_errors.append(str(e)))
            page.on("console", lambda m: report.console_errors.append(m.text) if m.type == "error" else None)
            await page.goto(url or html_path.as_uri(), wait_until="load")
            await page.wait_for_timeout(settle_ms)
            before = await _dom_hash(page)
            for step in steps:
                result = await _run_step(page, step, shots_dir, report)
                after = await _dom_hash(page)
                if step.split(" ", 1)[0] in ("click", "dblclick", "type", "press", "drag"):
                    result.changed = after != before
                elif step.strip() == "expect changed":
                    result.ok = after != before
                    result.note = "" if result.ok else "nothing in the DOM changed"
                before = after
                report.steps.append(result)
                if not result.ok:
                    report.ok = False
            await browser.close()
    except Exception as error:
        report.ok = False
        report.error = f"{type(error).__name__}: {str(error)[:200]}"
    return report


async def _run_step(page, step: str, shots_dir: Path | None, report: InteractionReport) -> StepResult:
    parts = step.split(" ", 2)
    verb = parts[0].lower()
    try:
        if verb == "click" and len(parts) >= 2:
            await page.locator(" ".join(parts[1:])).first.click(timeout=STEP_TIMEOUT_MS)
        elif verb == "dblclick" and len(parts) >= 2:
            await page.locator(" ".join(parts[1:])).first.dblclick(timeout=STEP_TIMEOUT_MS)
        elif verb == "type" and len(parts) >= 3:
            target = page.locator(parts[1]).first
            await target.click(timeout=STEP_TIMEOUT_MS)
            await target.type(parts[2], timeout=STEP_TIMEOUT_MS)
        elif verb == "press" and len(parts) >= 2:
            await page.keyboard.press(parts[1])
        elif verb == "drag" and len(parts) == 3:
            dx, dy = (int(v) for v in parts[2].split())
            box = await page.locator(parts[1]).first.bounding_box(timeout=STEP_TIMEOUT_MS)
            if box is None:
                return StepResult(step, False, "element has no box (not rendered?)")
            x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
            await page.mouse.move(x, y)
            await page.mouse.down()
            await page.mouse.move(x + dx / 2, y + dy / 2, steps=5)
            await page.mouse.move(x + dx, y + dy, steps=5)
            await page.mouse.up()
        elif verb == "wait" and len(parts) >= 2:
            await page.wait_for_timeout(int(parts[1]))
        elif verb == "expect" and len(parts) >= 2:
            if step.strip() == "expect changed":
                return StepResult(step, True)
            rest = step[len("expect "):]
            m = re.match(r"(.+?)\s+(visible|count|text)(?:\s+(.*))?$", rest)
            if not m:
                return StepResult(step, False, "expect needs: <selector> visible | count <n> | text <substring>")
            selector, what, arg = m.group(1), m.group(2), (m.group(3) or "")
            locator = page.locator(selector)
            if what == "visible":
                count = await locator.count()
                visible = count > 0 and await locator.first.is_visible()
                return StepResult(step, visible, "" if visible else f"{count} match(es), none visible")
            if what == "count":
                count = await locator.count()
                return StepResult(step, count == int(arg), f"{count} match(es)")
            text = (await locator.first.text_content(timeout=STEP_TIMEOUT_MS)) or "" if await locator.count() else ""
            ok = arg in text
            return StepResult(step, ok, "" if ok else f"text is {text.strip()[:80]!r}")
        elif verb == "shot" and len(parts) >= 2:
            if shots_dir is not None:
                shots_dir.mkdir(parents=True, exist_ok=True)
                target = shots_dir / f"{re.sub(r'[^A-Za-z0-9_-]+', '-', parts[1])}.png"
                await page.screenshot(path=str(target))
                report.screenshots.append(str(target))
        else:
            return StepResult(step, False, "unknown step (click/dblclick/type/press/drag/wait/expect/shot)")
        await page.wait_for_timeout(150)
        return StepResult(step, True)
    except Exception as error:
        return StepResult(step, False, f"{type(error).__name__}: {str(error).splitlines()[0][:140]}")


async def screenshot_set(html_path: Path, outdir: Path, steps: list[str] | None = None,
                         viewports: dict[str, tuple[int, int]] | None = None, url: str | None = None) -> dict:
    """The eval renderer's set: every viewport × top / middle / bottom,
    plus `after-<n>` shots following each interaction step, and the
    console log beside them. Returns {"images": [...], "console": path,
    "interaction": InteractionReport | None}."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"images": [], "console": "", "interaction": None, "error": "playwright not installed"}
    outdir.mkdir(parents=True, exist_ok=True)
    images: list[str] = []
    console: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        for name, (width, height) in (viewports or VIEWPORTS).items():
            page = await browser.new_page(viewport={"width": width, "height": height})
            page.on("pageerror", lambda e, n=name: console.append(f"[{n}] pageerror: {e}"))
            page.on("console", lambda m, n=name: console.append(f"[{n}] {m.type}: {m.text}"))
            await page.goto(url or html_path.as_uri(), wait_until="load")
            await page.wait_for_timeout(1200)
            total = await page.evaluate("() => Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)")
            for position, fraction in (("top", 0.0), ("middle", 0.5), ("bottom", 1.0)):
                await page.evaluate(f"() => window.scrollTo(0, {fraction} * Math.max(0, {total} - {height}))")
                await page.wait_for_timeout(250)
                target = outdir / f"{name}-{position}.png"
                await page.screenshot(path=str(target))
                images.append(str(target))
                if total <= height:
                    break                              # a page that fits: one shot is the truth
            await page.close()
        await browser.close()
    interaction = None
    if steps:
        shots = outdir / "interaction"
        scripted = []
        for index, step in enumerate(steps, 1):
            scripted.append(step)
            if step.split(" ", 1)[0] in ("click", "dblclick", "type", "press", "drag"):
                scripted.append(f"shot after-{index:02d}")
        interaction = await run_interactions(html_path, scripted, shots_dir=shots, url=url)
        if interaction:
            images.extend(interaction.screenshots)
            console.extend(f"[interaction] {e}" for e in interaction.console_errors)
    log = outdir / "console.log"
    log.write_text("\n".join(console) + ("\n" if console else ""), encoding="utf-8")
    return {"images": images, "console": str(log), "interaction": interaction}
