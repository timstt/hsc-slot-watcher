from __future__ import annotations

from datetime import date, datetime
from itertools import groupby

from watcher.config import Settings
from watcher.models import Slot

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]
WEEKDAYS_FULL = ["понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя"]
_RELATIVE = {0: "Сьогодні", 1: "Завтра", 2: "Післязавтра"}


def day_label(day: date, today: date) -> str:
    short = f"{WEEKDAYS[day.weekday()]}, {day:%d.%m}"
    rel = _RELATIVE.get((day - today).days)
    return f"{rel} ({short})" if rel else short.capitalize()


# Everything returned from this module goes into the Telegram message, so the
# user-facing strings (weekdays, headers, "о", "ще не було") are kept in Ukrainian.


def slot_lines(slots: list[Slot], s: Settings, today: date) -> list[str]:
    """One line per (date, center); times within a date are comma-separated."""
    names = s.center_names
    lines = []
    for (day, center_id), group in groupby(sorted(slots, key=lambda x: (x.day, x.center_id, x.start)),
                                           key=lambda x: (x.day, x.center_id)):
        times = [f"{x.start:%H:%M}" for x in group]
        when = f" о {times[0]}" if len(times) == 1 else ": " + ", ".join(times)
        lines.append(f"{day_label(day, today)}{when} — {names.get(center_id, center_id)}")
    return lines


def format_slots(slots: list[Slot], s: Settings, today: date, first_run: bool = False) -> str:
    if first_run:
        head = f"🚗 Зараз доступні слоти на теорію (до {s.latest_time:%H:%M})"
    elif len(slots) == 1:
        head = "🚗 Звільнився слот на теорію"
    else:
        head = "🚗 Звільнились слоти на теорію"
    return "\n".join([head, *slot_lines(slots, s, today), s.public_url])


def fmt_dt(value: str | None) -> str:
    if not value:
        return "ще не було"
    return datetime.fromisoformat(value).strftime("%d.%m %H:%M")
