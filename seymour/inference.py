"""Inference settings: how the model SAMPLES and how much it may think.

The knobs a person tuning a local model actually reaches for — temperature,
top-p / top-k / min-p, repeat penalty, thinking on/off/auto with a token
budget, the reply cap and the history budget — live here as ONE record,
persisted in app_state (product choices belong in the database, not .env)
and applied to every run: chat turns, agent steps, research. A message
may override them for itself (the composer's quick toggles); the run's
trace records the EFFECTIVE values, so a comparison between two runs is
never a guess about what the sampler was doing.

Defaults follow Qwen's own model card for Qwen3-class models: top_p 0.8,
top_k 20, min_p 0 for non-thinking use; presence penalty 0 (raise it only
when the model repeats itself); temperature 0.7. "auto" thinking keeps
the per-run policy the evals settled (chat off, agent steps on).
"""

from dataclasses import asdict, dataclass, fields

from seymour.db import get_state, set_state

# Every setting: (app_state key, default, min, max). Bounds are the sane
# range, not the engine's — a typo of 70 for temperature is a mistake,
# never a request.
_SPEC: dict[str, tuple[str, float | int | str, float | int | None, float | int | None]] = {
    "temperature":      ("inference_temperature", 0.7, 0.0, 2.0),
    "top_p":            ("inference_top_p", 0.8, 0.0, 1.0),
    "top_k":            ("inference_top_k", 20, 0, 400),
    "min_p":            ("inference_min_p", 0.0, 0.0, 1.0),
    "repeat_penalty":   ("inference_repeat_penalty", 1.0, 0.8, 2.0),
    "presence_penalty": ("inference_presence_penalty", 0.0, -2.0, 2.0),
    # "off" | "on" | "auto": auto = each run kind's measured default.
    "thinking":         ("inference_thinking", "auto", None, None),
    # Tokens the model may spend thinking when thinking is on; 0 = none,
    # -1 = unlimited. Only honored by engines that accept reasoning_budget.
    "reasoning_budget": ("inference_reasoning_budget", -1, -1, 65536),
    # The reply cap per model call (chat); agent steps keep their own.
    "max_tokens":       ("inference_max_tokens", 8192, 256, 32768),   # 4096 cut off ~300-line files (2026-09-02)
    # How much conversation HISTORY rides along, in tokens (estimated at
    # ~4 chars/token). Older turns fall off first; the latest turn always
    # stays. This is the per-conversation "context size" — the engine's
    # KV pool is a separate, load-time setting on the Engine card.
    "history_tokens":   ("inference_history_tokens", 24000, 2000, 200000),
}


@dataclass(frozen=True)
class Inference:
    """The effective inference settings for one run."""

    temperature: float
    top_p: float
    top_k: int
    min_p: float
    repeat_penalty: float
    presence_penalty: float
    thinking: str
    reasoning_budget: int
    max_tokens: int
    history_tokens: int

    def enable_thinking(self, default: bool) -> bool:
        """Resolve the three-way thinking switch against a run kind's
        measured default (chat: off, agent step: on)."""
        return {"on": True, "off": False}.get(self.thinking, default)

    def request_kwargs(self, *, thinking_default: bool) -> dict:
        """The GenerationRequest fields these settings decide. None-valued
        sampling fields would keep the engine default; every one here is
        set on purpose so the trace can say what was used."""
        thinking = self.enable_thinking(thinking_default)
        kwargs = {
            "temperature": self.temperature,
            "top_p": self.top_p, "top_k": self.top_k, "min_p": self.min_p,
            "repeat_penalty": self.repeat_penalty,
            "presence_penalty": self.presence_penalty,
            "template_kwargs": None if thinking else {"enable_thinking": False},
        }
        if thinking and self.reasoning_budget >= 0:
            kwargs["reasoning_budget"] = self.reasoning_budget
        return kwargs

    def trace(self, *, thinking_default: bool) -> dict:
        """What the run log records: the effective values, compactly."""
        return {**asdict(self), "thinking_effective": self.enable_thinking(thinking_default)}


def _coerce(name: str, raw, default):
    """Parse and clamp one value; anything unparseable falls back."""
    _, _, lo, hi = _SPEC[name]
    if isinstance(default, str):
        value = str(raw).strip().lower()
        return value if value in ("off", "on", "auto") else default
    try:
        value = int(raw) if isinstance(default, int) else float(raw)
    except (TypeError, ValueError):
        return default
    if lo is not None and value < lo:
        value = lo
    if hi is not None and value > hi:
        value = hi
    return value


def current(overrides: dict | None = None) -> Inference:
    """The saved settings (else defaults), with per-message overrides
    applied on top — a composer toggle wins for its own message only."""
    values = {}
    for name, (key, default, _lo, _hi) in _SPEC.items():
        saved = get_state(key)
        values[name] = default if saved is None else _coerce(name, saved, default)
    for name, raw in (overrides or {}).items():
        if name in _SPEC and raw is not None:
            values[name] = _coerce(name, raw, _SPEC[name][1])
    return Inference(**values)


def save(changes: dict) -> Inference:
    """Persist settings (validated + clamped). Takes effect on the next
    request — nothing is latched onto a conversation."""
    for name, raw in changes.items():
        if name not in _SPEC or raw is None:
            continue
        key, default, _lo, _hi = _SPEC[name]
        set_state(key, str(_coerce(name, raw, default)))
    return current()


def defaults() -> dict:
    """The factory defaults, for the UI's reset control."""
    return {name: spec[1] for name, spec in _SPEC.items()}


def bounds() -> dict:
    """[min, max] per numeric setting, so the UI can render honest inputs."""
    return {name: [spec[2], spec[3]] for name, spec in _SPEC.items()
            if spec[2] is not None}


def budget_history(history: list[dict], limit_tokens: int) -> list[dict]:
    """Trim a conversation's prior turns to the history budget: oldest
    first, whole messages, at ~4 characters per token. The most recent
    message is never dropped."""
    if not history:
        return history
    budget = max(0, limit_tokens) * 4
    kept: list[dict] = []
    used = 0
    for message in reversed(history):
        size = len(str(message.get("content", "")))
        if kept and used + size > budget:
            break
        kept.append(message)
        used += size
    return list(reversed(kept))


__all__ = ["Inference", "current", "save", "defaults", "bounds", "budget_history",
           "fields"]
