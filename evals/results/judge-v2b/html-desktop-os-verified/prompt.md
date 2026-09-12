You are a blind referee for rendered deliverables produced by an automated assistant. You see: the task prompt that produced the deliverable(s), a rubric, and rendered images. You know nothing about what produced them, and you must not guess.

## The task prompt (what was asked)

Create os.html in the workspace: a single self-contained HTML file (vanilla HTML/CSS/JS, no external resources) that simulates a small desktop operating system: a wallpaper, a taskbar with a live clock and a start button (id="start-button") that toggles a start menu (id="start-menu") listing apps as elements with data-app="notepad", data-app="calculator" and data-app="about"; clicking an app opens it in a draggable window (class="window", dragged by its child with class="titlebar") that can be closed and minimised; clicking a window brings it to the front. The notepad has a textarea id="notepad-text". The calculator has a display id="calc-display" and buttons with data-key for the digits, operators (+ - * /), ".", "=" and "C", and must compute correctly (8 / 2 = 4). Make it look clean and modern. Write the file, then verify it actually works (check_page with interaction steps, or a headless browser) before you finish.

## Rubric (score each criterion 1–10)

1. Layout and visual polish at desktop width (spacing, alignment, colour)
2. Holds up on the laptop and phone viewports (no overflow, no unreadable text)
3. The interactions asked for visibly work (compare the after-interaction images)
4. No console errors; nothing visibly broken
5. Would a person use this?

## The 10-point scale is anchored, not vibes

- 10 = professional enough to present to a client with no edits.
- 7 = correct and clean, visibly machine-made.
- 4 = the content is right, the presentation is not.
- 1 = broken or empty.

## What you are given

### Artifact A
Images (look at every one): A/01.png, A/02.png, A/03.png, A/04.png, A/05.png, A/06.png, A/07.png, A/08.png, A/09.png, A/10.png, A/11.png, A/12.png, A/13.png, A/14.png, A/15.png, A/16.png
Interaction results (✔ passed, ✘ failed):
```
✔ expect #start-button visible
✔ click #start-button · DOM changed
✔ shot after-02
✔ expect #start-menu visible
✔ click [data-app=notepad] · DOM changed
✔ shot after-04
✔ expect #notepad-text visible
✔ type #notepad-text hello from the eval · DOM changed
✔ shot after-06
✔ expect #notepad-text visible
✔ click #start-button · DOM changed
✔ shot after-08
✔ click [data-app=calculator] · DOM changed
✔ shot after-09
✔ expect #calc-display visible
✔ click [data-key='8'] · DOM changed
✔ shot after-11
✔ click [data-key='/'] · DOM changed
✔ shot after-12
✔ click [data-key='2'] · DOM changed
✔ shot after-13
✔ click [data-key='='] · DOM changed
✔ shot after-14
✔ expect #calc-display text 4
✔ drag .window .titlebar 120 60 · DOM changed
✔ shot after-16
✘ expect changed — nothing in the DOM changed
✔ click #start-button · DOM changed
✔ shot after-18
✔ click [data-app=about] · DOM changed
✔ shot after-19
✘ click .window · DOM unchanged — TimeoutError: Locator.click: Timeout 4000ms exceeded.
✔ shot after-20
✘ expect changed — nothing in the DOM changed
```

## Rules

- Look at every image before scoring. For pages, the console log is evidence too.
- Every criterion score MUST cite specific visual evidence: name the image and what is in it ("slide 3 (A/03.png): the bullet text runs past the right edge of its box"). A justification that does not reference the images is invalid.
- Judge what is rendered, not what might have been intended.
- When there are two artifacts, they were made from the same prompt; compare them on the same criteria and pick a winner (or "tie" only when every criterion is within 1 point).

## Output

Reply with ONLY this JSON, no prose around it:

{
  "artifacts": {
    "A": {
      "criteria": [{"name": "<criterion>", "score": <1-10>, "evidence": "<one line citing an image>"}],
      "overall": <1-10 integer>,
      "highest_leverage_fix": "<the single change that would raise the score most>"
    }
    /* , "B": {...} when a second artifact was given */
  },
  "winner": "A" | "B" | "tie" | null,
  "confidence": "high" | "medium" | "low"
}
