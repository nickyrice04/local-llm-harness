"""Battery awareness: a background agent on a laptop is a guest.

The agent should not drain a battery at full tilt while the lid is open in
a coffee shop. This module answers one question — "are we on battery?" —
and the manager slows the agent's step cadence accordingly, SAYING SO in
the UI (the honesty rule: a slowed agent the user understands is fine; a
mysteriously slow one is a bug report).
"""

# psutil reads the battery state portably (and returns None on desktops).
import psutil


def on_battery() -> bool:
    """True when running unplugged. Desktops and any errors read as
    plugged-in — the agent should only ever slow down on solid evidence."""
    try:
        battery = psutil.sensors_battery()
    except Exception:
        return False
    if battery is None:              # no battery: a desktop Mac
        return False
    return not battery.power_plugged


def battery_percent() -> int | None:
    """Charge percentage for the status UI, or None when there's no battery."""
    try:
        battery = psutil.sensors_battery()
    except Exception:
        return None
    return int(battery.percent) if battery else None
