"""Parsing JSON out of LLM output — grubby, and in one place only.

Every extractor in the reference implementation independently re-learned the
same lessons: models wrap JSON in ```json fences, lead with <think> blocks
and prose, echo the prompt's example array, and get truncated mid-output.
This module owns those lessons so no caller has to.
"""

# json for actual parsing; re for the cleanup passes.
import json
import re


def _strip_noise(text: str) -> str:
    """Remove the wrappers models put around JSON."""
    # <think>…</think> reasoning blocks come BEFORE the answer; drop them.
    # (An unclosed think block means truncation — drop to end.)
    text = re.sub(r"<think>.*?(</think>|\Z)", "", text, flags=re.DOTALL)
    # Markdown code fences (```json … ``` or bare ``` … ```).
    text = re.sub(r"```(?:json)?", "", text)
    return text.strip()


def parse_json_array(text: str) -> list:
    """Extract the LAST parseable JSON array from model output.

    The LAST, because prompts show an example array and models sometimes
    echo it before writing the real one. Falls back to bracket-repair for
    truncated output, then to an empty list — a failed extraction should
    cost nothing, never crash anything.
    """
    text = _strip_noise(text)
    # Find every top-level-looking array candidate, last first.
    candidates = re.findall(r"\[.*?\]", text, flags=re.DOTALL)
    for candidate in reversed(candidates):
        try:
            result = json.loads(candidate)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            continue
    # Truncation repair: take from the last '[', close it, retry with
    # trailing garbage progressively trimmed at commas.
    start = text.rfind("[")
    if start != -1:
        fragment = text[start:]
        while fragment:
            try:
                result = json.loads(fragment + "]")
                if isinstance(result, list):
                    return result
            except json.JSONDecodeError:
                pass
            # Trim back to the previous comma and try closing there.
            cut = fragment.rfind(",")
            if cut == -1:
                break
            fragment = fragment[:cut]
    return []


def parse_json_object(text: str) -> dict | None:
    """Extract the first complete JSON OBJECT from model output, or None.

    Used for in-band tool calls: the agent replies with {"tool": …,
    "args": {…}} and this digs it out from any surrounding prose. A
    brace-depth scan (rather than a regex) handles nested objects in the
    args correctly.
    """
    text = _strip_noise(text)
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        # Walk forward counting braces until this candidate closes —
        # ignoring braces INSIDE strings. Measured 2026-09-03: a
        # write_file whose content held CSS ("body { display: flex; }")
        # closed the candidate at the wrong brace, json.loads saw an
        # unterminated string, and every whole-page call "failed to
        # parse" (4,760 tokens once, 431 tokens twice).
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    result = _loads_lenient(text[start:i + 1])
                    if isinstance(result, dict):
                        return result
                    break             # malformed — try the next '{'
        # This candidate failed; look for the next opening brace.
        start = text.find("{", start + 1)
    return None


# Escapes JSON allows; anything else after a backslash is the model's
# habit (\' most often) and is repaired by dropping the backslash.
_BAD_ESCAPE = re.compile(r'\\(?!["\\/bfnrtu])')


def _loads_lenient(candidate: str) -> dict | None:
    """json.loads with the two forgivenesses local models need: raw
    control characters inside strings (strict=False) and invalid escapes
    such as \' — never any change to what the text MEANS."""
    for attempt in (candidate, _BAD_ESCAPE.sub("", candidate)):
        try:
            return json.loads(attempt, strict=False)
        except json.JSONDecodeError:
            continue
    return None
