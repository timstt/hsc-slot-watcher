from datetime import date, datetime, time

from watcher.core.filters import interesting, is_interesting, new_slots

from .conftest import D1, D2, KYIV, NOW, slot


def test_diff_new_is_current_minus_previous():
    prev = {slot(D1, "11:00"), slot(D1, "11:15")}
    cur = {slot(D1, "11:15"), slot(D1, "09:00")}
    assert new_slots(prev, cur) == {slot(D1, "09:00")}


def test_diff_first_run_everything_is_new():
    cur = {slot(D1, "09:00")}
    assert new_slots(None, cur) == cur


def test_diff_empty_previous_is_not_first_run():
    assert new_slots(set(), {slot(D1, "09:00")}) == {slot(D1, "09:00")}


def test_threshold_inclusive(settings):
    assert is_interesting(slot(D1, "09:00"), NOW, settings)
    assert is_interesting(slot(D1, "10:30"), NOW, settings)
    assert not is_interesting(slot(D1, "10:31"), NOW, settings)
    assert not is_interesting(slot(D1, "10:45"), NOW, settings)


def test_threshold_exclusive(settings):
    s = settings.model_copy(update={"latest_inclusive": False})
    assert is_interesting(slot(D1, "10:15"), NOW, s)
    assert not is_interesting(slot(D1, "10:30"), NOW, s)


def test_threshold_configurable(settings):
    s = settings.model_copy(update={"latest_time": time(9, 0)})
    assert is_interesting(slot(D1, "09:00"), NOW, s)
    assert not is_interesting(slot(D1, "09:15"), NOW, s)


def test_date_range(settings):
    s = settings.model_copy(update={"date_from": D2, "date_to": D2})
    assert not is_interesting(slot(D1, "09:00"), NOW, s)
    assert is_interesting(slot(D2, "09:00"), NOW, s)
    assert not is_interesting(slot(date(2026, 11, 14), "09:00"), NOW, s)


def test_past_and_too_soon_ignored(settings):
    today = NOW.date()
    morning = datetime(2026, 11, 11, 7, 0, tzinfo=KYIV)
    assert not is_interesting(slot(date(2026, 11, 10), "09:00"), morning, settings)  # yesterday
    assert not is_interesting(slot(today, "08:59"), morning, settings)  # less than 2 h away
    assert is_interesting(slot(today, "09:00"), morning, settings)  # exactly 2 h
    assert not is_interesting(slot(today, "06:00"), morning, settings)  # already passed


def test_interesting_sorted_by_date_and_time(settings):
    slots = {slot(D2, "08:00"), slot(D1, "10:00"), slot(D1, "09:00"), slot(D1, "12:00")}
    assert interesting(slots, NOW, settings) == [
        slot(D1, "09:00"), slot(D1, "10:00"), slot(D2, "08:00"),
    ]
