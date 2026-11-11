"""Test mode: fake site responses are run through the whole chain.

The MVS site is not touched at all here: FakeClient returns responses in the
real API format (see tests/fixtures), and from there the same code runs as in
the real monitor: parser -> diff -> filter -> notify.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from watcher.config import Settings
from watcher.models import RawResponse, SlotClient
from watcher.notifier.base import Notifier, RecordingNotifier
from watcher.scheduler.runner import Monitor
from watcher.storage.db import Database

BROKEN = "broken"      # HTTP 500 with an empty body
EMPTY_BODY = "empty"   # HTTP 200, but the body is empty (not JSON)
EXPIRED = "expired"    # HTTP 401 "Unauthorized"
BLOCKED = "blocked"    # HTTP 403 Access Denied

Step = dict[date, list[str]] | str


class FakeClient(SlotClient):
    """Returns responses like the real API. state -- a schedule {date: ["HH:MM", ...]} or a failure mode."""

    def __init__(self, state: Step | None = None) -> None:
        self.state: Step = state or {}
        self.requests: list[str] = []

    def get(self, path: str) -> RawResponse:
        self.requests.append(path)
        if self.state == BROKEN:
            return RawResponse(500, "text/html", "")
        if self.state == EMPTY_BODY:
            return RawResponse(200, "application/json", "")
        if self.state == EXPIRED:
            return RawResponse(401, "application/json", '"Unauthorized"')
        if self.state == BLOCKED:
            return RawResponse(403, "text/html", "<HTML><TITLE>Access Denied</TITLE></HTML>")
        assert isinstance(self.state, dict)
        if "/equeue/departments" in path:
            data = [{"id": 142}]
        elif "/equeue/days" in path:
            data = [{"date": f"{d.isoformat()}T00:00:00"} for d in sorted(self.state)]
        else:
            day = date.fromisoformat(re.search(r"date=(\d{4}-\d{2}-\d{2})", path).group(1))
            data = []
            for hhmm in self.state.get(day, []):
                start = datetime.strptime(hhmm, "%H:%M")
                stop = start + timedelta(minutes=15)
                data.append({"startTime": f"{start:%H:%M}:00", "stopTime": f"{stop:%H:%M}:00"})
        return RawResponse(200, "application/json", json.dumps({"data": data}))


@dataclass
class Scenario:
    title: str
    baseline: Step                      # snapshot before the start (no notifications)
    steps: list[tuple[Step, int]]       # (site state, how many messages we expect)
    must_contain: list[str] = field(default_factory=list)
    must_not_contain: list[str] = field(default_factory=list)


def scenarios(today: date) -> list[Scenario]:
    d1, d2 = today + timedelta(days=1), today + timedelta(days=2)
    late = {d1: ["11:00", "11:15"], d2: ["12:00"]}

    def plus(extra: dict[date, list[str]]) -> dict[date, list[str]]:
        out = {k: list(v) for k, v in late.items()}
        for k, v in extra.items():
            out[k] = sorted(out.get(k, []) + v)
        return out

    return [
        Scenario("1. New slot at 09:00 -> notification", late, [(plus({d1: ["09:00"]}), 1)], ["09:00"]),
        Scenario("2. Slot exactly at 10:30 -> notification", late, [(plus({d1: ["10:30"]}), 1)], ["10:30"]),
        Scenario("3. Slots at 10:31, 10:45, 13:00 -> silence", late,
                 [(plus({d1: ["10:31", "10:45", "13:00"]}), 0)]),
        Scenario("4. Slot disappeared and came back -> two notifications", late,
                 [(plus({d1: ["09:00"]}), 1), (late, 0), (plus({d1: ["09:00"]}), 1)], ["09:00"]),
        Scenario("5. One date with slots before and after the threshold -> only the ones before", late,
                 [(plus({d2: ["08:45", "09:15", "10:45", "14:00"]}), 1)],
                 ["08:45", "09:15"], ["10:45", "14:00", "12:00"]),
        Scenario("6. Broken/empty response -> snapshot untouched, no false notifications",
                 plus({d1: ["09:00"]}),
                 [(BROKEN, 0), (EMPTY_BODY, 0), (plus({d1: ["09:00"]}), 0)]),
        Scenario("7. Expired session -> one service message", late,
                 [(EXPIRED, 1), (EXPIRED, 0)], ["python -m watcher login"]),
    ]


def run(settings: Settings, notifier: Notifier, prefix: str = "[ТЕСТ] ") -> bool:
    """Run all scenarios. Returns True if all matched expectations."""
    now = settings.now().replace(hour=12, minute=0, second=0, microsecond=0)
    # The test must not depend on the time of day: disable the heartbeat window and quiet hours.
    s = settings.model_copy(update={"heartbeat_time": datetime.max.time(), "quiet_enabled": False})
    all_ok = True
    for sc in scenarios(now.date()):
        db = Database(":memory:")
        client = FakeClient(sc.baseline)
        Monitor(client, db, RecordingNotifier(), s, now_fn=lambda: now).step()  # baseline snapshot
        rec = RecordingNotifier(notifier)
        monitor = Monitor(client, db, rec, s, now_fn=lambda: now, prefix=prefix)
        ok = True
        print(f"\n=== {sc.title}")
        for i, (state, expected) in enumerate(sc.steps, 1):
            before = len(rec.sent)
            client.state = state
            monitor.step()
            got = len(rec.sent) - before
            good = got == expected
            ok &= good
            print(f"  step {i}: messages {got}, expected {expected} -- {'OK' if good else 'FAIL'}")
        text = "\n".join(t for t, _ in rec.sent)
        for needle in sc.must_contain:
            if needle not in text:
                ok = False
                print(f"  message is missing '{needle}' -- FAIL")
        for needle in sc.must_not_contain:
            if needle in text:
                ok = False
                print(f"  message has an extra '{needle}' -- FAIL")
        print(f"  result: {'OK' if ok else 'FAIL'}")
        all_ok &= ok
        db.close()
    return all_ok
