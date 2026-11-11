"""Prioritized date scanning and partial snapshots (because of the site rate limit)."""
from datetime import date, datetime, timedelta

from watcher.core.cycle import choose_days, run_cycle
from watcher.notifier.base import RecordingNotifier
from watcher.simulate import FakeClient

from .conftest import KYIV, NOW

DAYS = [date(2026, 11, 12) + timedelta(days=i) for i in range(15)]
RUSH = datetime(2026, 11, 11, 6, 1, tzinfo=KYIV)


def tiered(settings, **kw):
    return settings.model_copy(update={"scan_mode": "tiered", "near_dates": 3, "far_per_cycle": 2, **kw})


def slots_requested(client):
    return [p.split("date=")[1][:10] for p in client.requests if "/slots" in p]


# --- date selection ---
def test_full_mode_reads_everything(settings):
    assert choose_days(DAYS, set(), False, settings, 0) == (DAYS, 0)


def test_tiered_near_every_cycle_rest_in_rotation(settings):
    s = tiered(settings)
    seen_far = []
    cursor = 0
    for _ in range(6):
        picked, cursor = choose_days(DAYS, set(DAYS), False, s, cursor)
        assert picked[:3] == DAYS[:3]  # the nearest -- every cycle
        assert len(picked) == 5
        seen_far += picked[3:]
    assert sorted(seen_far) == DAYS[3:]  # over 6 cycles all 12 far dates are covered
    assert cursor == 0


def test_tiered_new_edge_date_not_prioritized_by_default(settings):
    s = tiered(settings)
    picked, _ = choose_days(DAYS, set(DAYS[:-1]), False, s, 0)
    assert picked == DAYS[:5]  # a new edge date waits for its turn


def test_tiered_returned_date_read_immediately(settings):
    s = tiered(settings)
    listed = set(DAYS) - {DAYS[9]}  # the date was fully booked and came back
    picked, _ = choose_days(DAYS, listed, False, s, 0)
    assert DAYS[9] in picked


def test_tiered_new_edge_date_read_immediately_if_enabled(settings):
    s = tiered(settings, notify_new_dates=True)
    known = set(DAYS[:-1])
    picked, _ = choose_days(DAYS, known, False, s, 0)
    assert DAYS[-1] in picked


def test_rush_reads_only_edge_and_new(settings):
    settings = settings.model_copy(update={"rush_enabled": True, "notify_new_dates": True})
    picked, cursor = choose_days(DAYS, set(DAYS[:-1]), True, settings, 4)
    assert picked == [DAYS[-1]] and cursor == 4
    picked, _ = choose_days(DAYS, set(DAYS[:-2]), True, settings, 0)
    assert picked == DAYS[-2:]


def test_no_days_no_requests(settings):
    assert choose_days([], set(), False, tiered(settings), 3) == ([], 3)


# --- partial snapshot ---
def test_tiered_cycle_makes_few_requests_and_keeps_other_days(db, settings):
    s = tiered(settings)
    state = {d: ["11:00"] for d in DAYS}
    client = FakeClient(state)
    run_cycle(client, db, None, s, NOW)
    assert slots_requested(client) == [d.isoformat() for d in DAYS[:5]]
    client.requests.clear()
    run_cycle(client, db, None, s, NOW)
    assert slots_requested(client) == [d.isoformat() for d in DAYS[:3] + DAYS[5:7]]
    # dates not read in this cycle did not vanish from the snapshot
    assert {x.day for x in db.get_snapshot(142)} == set(DAYS[:7])


def test_unread_day_is_not_treated_as_disappeared(db, settings):
    """A slot on a date outside the current cycle does not "disappear" or "reappear"."""
    s = tiered(settings, near_dates=1, far_per_cycle=1)
    state = {DAYS[0]: ["11:00"], DAYS[1]: ["09:00"], DAYS[2]: ["11:00"]}
    client = FakeClient(state)
    rec = RecordingNotifier()
    for _ in range(6):
        run_cycle(client, db, rec, s, NOW)
    assert len(rec.sent) == 1  # 09:00 on DAYS[1] -- exactly one message
    assert "Зараз доступні" in rec.sent[0][0]  # date read for the first time -- not "released"


def test_released_slot_on_known_far_day_notifies_when_its_turn_comes(db, settings):
    s = tiered(settings, near_dates=1, far_per_cycle=1)
    state = {d: ["11:00"] for d in DAYS[:3]}
    client = FakeClient(state)
    rec = RecordingNotifier()
    for _ in range(2):
        run_cycle(client, db, rec, s, NOW)  # went through all dates
    client.state = {**state, DAYS[2]: ["09:00", "11:00"]}
    run_cycle(client, db, rec, s, NOW)  # DAYS[1]'s turn -- still silent
    assert rec.sent == []
    run_cycle(client, db, rec, s, NOW)  # DAYS[2]'s turn
    assert len(rec.sent) == 1 and "Звільнився слот" in rec.sent[0][0]


def test_new_edge_date_is_silent_by_default_but_later_release_notifies(db, settings):
    state = {d: ["11:00"] for d in DAYS[:-1]}
    client = FakeClient(state)
    rec = RecordingNotifier()
    run_cycle(client, db, rec, settings, NOW)
    opened = {**state, DAYS[-1]: ["08:00", "09:30", "12:00"]}  # a new date opened
    client.state = opened
    run_cycle(client, db, rec, settings, NOW)
    assert rec.sent == []  # a new date is always free -- stay silent
    client.state = {**opened, DAYS[-1]: ["12:00"]}  # morning ones taken
    run_cycle(client, db, rec, settings, NOW)
    client.state = {**opened, DAYS[-1]: ["09:30", "12:00"]}  # someone cancelled 09:30
    run_cycle(client, db, rec, settings, NOW)
    assert len(rec.sent) == 1 and "Звільнився слот" in rec.sent[0][0] and "09:30" in rec.sent[0][0]


def test_rush_cycle_two_requests_and_new_date_notifies(db, settings):
    settings = settings.model_copy(update={"rush_enabled": True, "notify_new_dates": True})
    state = {d: ["11:00"] for d in DAYS[:-1]}
    client = FakeClient(state)
    run_cycle(client, db, None, settings, NOW)  # a normal full cycle the day before
    client.requests.clear()

    client.state = {**state, DAYS[-1]: ["08:00", "09:30", "12:00"]}  # a new date opened at 06:00
    rec = RecordingNotifier()
    run_cycle(client, db, rec, settings, RUSH)
    assert slots_requested(client) == [DAYS[-1].isoformat()]
    assert len(client.requests) == 2  # days + one date
    text = rec.sent[0][0]
    assert "08:00, 09:30" in text and "12:00" not in text
    assert rec.sent[0][1] is False  # 06:01 -- no longer quiet hours

    run_cycle(client, db, rec, settings, RUSH + timedelta(minutes=1))
    assert len(rec.sent) == 1  # we do not write again


def test_day_removed_from_site_is_dropped_from_snapshot(db, settings):
    client = FakeClient({DAYS[0]: ["09:00"], DAYS[1]: ["11:00"]})
    run_cycle(client, db, None, settings, NOW)
    client.state = {DAYS[1]: ["11:00"]}
    run_cycle(client, db, None, settings, NOW)
    assert {x.day for x in db.get_snapshot(142)} == {DAYS[1]}
    assert db.known_days(142) == {DAYS[1]}


def test_full_flag_ignores_rush_and_tiered(db, settings):
    client = FakeClient({d: ["11:00"] for d in DAYS})
    run_cycle(client, db, None, tiered(settings), RUSH, full=True)
    assert len(slots_requested(client)) == 15
