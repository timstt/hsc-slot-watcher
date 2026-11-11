from __future__ import annotations

from datetime import datetime, timedelta

from watcher.config import Settings
from watcher.models import Slot


def new_slots(previous: set[Slot] | None, current: set[Slot]) -> set[Slot]:
    """Slots present now that were not in the previous snapshot.

    previous=None means the first run: everything current counts as "new".
    A slot that disappeared and came back shows up here again -- as intended.
    """
    return set(current) if previous is None else current - previous


def is_interesting(slot: Slot, now: datetime, s: Settings) -> bool:
    """Whether to show the slot to the user (time threshold, date range, 2 h lead)."""
    if s.latest_inclusive:
        if slot.start > s.latest_time:
            return False
    elif slot.start >= s.latest_time:
        return False
    if s.date_from and slot.day < s.date_from:
        return False
    if s.date_to and slot.day > s.date_to:
        return False
    return slot.starts_at(s.tz) - now >= timedelta(minutes=s.min_lead_minutes)


def interesting(slots: set[Slot], now: datetime, s: Settings) -> list[Slot]:
    return sorted(x for x in slots if is_interesting(x, now, s))
