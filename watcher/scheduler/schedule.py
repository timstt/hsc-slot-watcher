from __future__ import annotations

import random
from datetime import datetime, time, timedelta

from watcher.config import Settings

RUSH_JITTER = 0.1


def in_window(t: time, start: time, end: time) -> bool:
    """start inclusive, end exclusive; the window may wrap over midnight."""
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def mode(t: time, s: Settings) -> str:
    if s.rush_enabled and in_window(t, s.rush_start, s.rush_end):
        return "rush"
    if s.night_enabled and in_window(t, s.night_start, s.night_end):
        return "night"
    return "normal"


def is_quiet(t: time, s: Settings) -> bool:
    return s.quiet_enabled and in_window(t, s.quiet_start, s.quiet_end)


def next_delay(now: datetime, s: Settings, rng: random.Random) -> float:
    """How many seconds to wait until the next cycle."""
    m = mode(now.time(), s)
    if m == "rush":
        base, jitter = s.rush_interval_seconds, RUSH_JITTER
    elif m == "night":
        base, jitter = s.night_interval_seconds, s.interval_jitter
    else:
        base, jitter = s.interval_seconds, s.interval_jitter
    delay = base * (1 + rng.uniform(-jitter, jitter))

    # Do not oversleep the start of the new-date window.
    if s.rush_enabled and m != "rush":
        start = now.replace(
            hour=s.rush_start.hour, minute=s.rush_start.minute, second=0, microsecond=0
        )
        if start <= now:
            start += timedelta(days=1)
        delay = min(delay, (start - now).total_seconds())
    return max(delay, 1.0)
