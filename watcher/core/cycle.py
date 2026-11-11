"""One monitoring cycle: snapshot -> diff -> filter -> notify -> persist.

This only READS from the site: the GET endpoints departments, days, slots.
The booking endpoint is never called anywhere in the project.

The site rate-limits requests (see docs/findings.md §7), so a cycle may read
only a subset of dates (see choose_days). The diff is computed only over the
dates read in this cycle; the rest of the snapshot is left untouched.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable

from watcher.config import Settings
from watcher.core.filters import interesting
from watcher.models import Slot, SlotClient
from watcher.notifier.base import Notifier
from watcher.notifier.format import format_slots
from watcher.parser.api import (
    days_path, decode, departments_path, parse_days, parse_slots, slots_path,
)
from watcher.scheduler.schedule import is_quiet, mode
from watcher.storage.db import Database

log = logging.getLogger(__name__)


@dataclass
class CycleResult:
    center_id: int
    snapshot: set[Slot]      # full persisted snapshot after the cycle
    covered: set[date]       # dates read in this cycle
    fresh: list[Slot]        # new interesting slots (<= threshold), sorted
    first_run: bool
    notified: bool


@dataclass
class Fetched:
    slots: set[Slot]
    covered: set[date]
    all_days: set[date]


def choose_days(
    days: list[date], listed: set[date], rush: bool, s: Settings, cursor: int
) -> tuple[list[date], int]:
    """Which dates to read this cycle. Returns (dates, new rotation cursor).

    - rush window (disabled by default): only edge dates and just-opened ones;
    - scan_mode=full: all dates;
    - scan_mode=tiered: the nearest near_dates every cycle (that's where
      cancellations show up), the rest in rotation, far_per_cycle per cycle;
      a just-opened date is read immediately.
    """
    if not days:
        return [], cursor
    # `listed` are the dates the site showed in the previous cycle. A date that
    # reappears in the list (it was fully booked, then someone cancelled) is read
    # immediately. A brand-new edge date only if notify_new_dates: at 06:00 it is
    # always free, so checking it separately makes no sense.
    newly = [d for d in days if d not in listed and (s.notify_new_dates or d < max(listed))] if listed else []
    if rush:
        edge = days[-s.edge_dates:] if s.edge_dates > 0 else []
        return sorted(set(edge) | set(newly)), cursor
    if s.scan_mode == "full":
        return list(days), cursor
    near = days[: s.near_dates]
    rest = [d for d in days[s.near_dates:] if d not in newly]
    take: list[date] = []
    if rest and s.far_per_cycle > 0:
        n = min(s.far_per_cycle, len(rest))
        take = [rest[(cursor + i) % len(rest)] for i in range(n)]
        cursor = (cursor + n) % len(rest)
    return sorted(set(near) | set(newly) | set(take)), cursor


def _get(client: SlotClient, path: str):
    resp = client.get(path)
    log.debug("GET %s -> %s", path.split("/equeue/")[-1], resp.status)
    return resp


def fetch_snapshot(
    client: SlotClient,
    center_id: int,
    s: Settings,
    pause: Callable[[], None],
    choose: Callable[[list[date]], list[date]] | None = None,
) -> Fetched:
    """Slots of a center across the chosen dates. Requests are strictly serial.

    Any error aborts the snapshot with an exception -- a partial result is never
    returned, so the diff cannot mistakenly conclude that slots disappeared.
    """
    # The site returns dates only after the "pick a center" step (as a human does
    # in the UI): without it `days` returns 204 with no body. So the first cycle
    # starts with that step, and if the site "forgets" it, we repeat it once.
    if not client.warmed:
        decode(_get(client, departments_path(s.service_id)))
        client.warmed = True
        pause()
    resp = _get(client, days_path(s.service_id, center_id))
    if resp.status == 204:
        log.info("days returned 204 -- repeating the center-selection step")
        pause()
        decode(_get(client, departments_path(s.service_id)))
        pause()
        resp = _get(client, days_path(s.service_id, center_id))
    days = parse_days(resp)
    raw_by_day = dict(days)
    all_days = [d for d, _ in days]
    wanted = choose(all_days) if choose else all_days
    slots: set[Slot] = set()
    for day in wanted:
        pause()
        slots |= parse_slots(
            _get(client, slots_path(s.service_id, center_id, raw_by_day[day])), center_id, day
        )
    return Fetched(slots, set(wanted), set(all_days))


def run_cycle(
    client: SlotClient,
    db: Database,
    notifier: Notifier | None,
    s: Settings,
    now: datetime,
    pause: Callable[[], None] = lambda: None,
    prefix: str = "",
    full: bool = False,
) -> list[CycleResult]:
    """full=True -- read every date regardless of scan mode (the `once` command)."""
    results: list[CycleResult] = []
    paused = db.kv_get("paused") == "1"
    rush = not full and mode(now.time(), s) == "rush"
    for i, center in enumerate(s.service_centers):
        if i:
            pause()
        known = db.known_days(center.id)
        listed_key = f"listed:{center.id}"
        listed = {date.fromisoformat(x) for x in json.loads(db.kv_get(listed_key, "[]"))}
        cursor_key = f"cursor:{center.id}"
        cursor = int(db.kv_get(cursor_key, "0") or 0)
        new_cursor = cursor

        def choose(days: list[date]) -> list[date]:
            nonlocal new_cursor
            if full:
                return days
            picked, new_cursor = choose_days(days, listed, rush, s, cursor)
            return picked

        fetched = fetch_snapshot(client, center.id, s, pause, choose)
        # Just-opened edge dates not read before: their slots are not "released",
        # they simply opened, so we stay silent about them (unless notify_new_dates).
        silent_key = f"silent:{center.id}"
        silent = {date.fromisoformat(x) for x in json.loads(db.kv_get(silent_key, "[]"))}
        if listed:
            silent |= {d for d in fetched.all_days if d not in listed and d > max(listed)}
        if s.notify_new_dates:
            silent = set()
        previous = db.get_snapshot(center.id)
        first_run = previous is None
        previous_covered = {x for x in (previous or set()) if x.day in fetched.covered}
        fresh = [x for x in interesting(fetched.slots - previous_covered, now, s)
                 if x.day not in silent]
        # "Released" only on a date we have seen before. Slots on a date read for
        # the first time (first run or a just-opened date) count as "available".
        released = [x for x in fresh if x.day in known]

        notified = False
        if fresh and notifier is not None and not paused:
            text = prefix + format_slots(fresh, s, now.date(), first_run=not released)
            # If sending fails, the exception propagates, the snapshot is not
            # saved, and the next cycle sees these slots as new again.
            notifier.send(text, silent=is_quiet(now.time(), s))
            notified = True
        if released:
            db.record_appearances(released, now)
        if notified and not first_run:
            count = int(db.kv_get("notified_since_hb", "0") or 0)
            db.kv_set("notified_since_hb", str(count + len(fresh)))

        db.apply_partial(center.id, fetched.covered, fetched.slots, fetched.all_days)
        db.kv_set(cursor_key, str(new_cursor))
        still_silent = (silent & fetched.all_days) - fetched.covered
        db.kv_set(silent_key, json.dumps(sorted(d.isoformat() for d in still_silent)))
        db.kv_set(listed_key, json.dumps(sorted(d.isoformat() for d in fetched.all_days)))
        log.info(
            "center %s: read %d of %d dates, %d slots, %d new <= threshold%s",
            center.name, len(fetched.covered), len(fetched.all_days), len(fetched.slots),
            len(fresh), " (first run)" if first_run else "",
        )
        results.append(CycleResult(
            center.id, db.get_snapshot(center.id) or set(), fetched.covered, fresh,
            first_run, notified,
        ))

    db.kv_set("last_success", now.isoformat())
    db.kv_set("session_ok", "1")
    return results
