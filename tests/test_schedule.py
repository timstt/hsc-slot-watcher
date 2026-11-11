import random
from datetime import datetime, time

import pytest

from watcher.scheduler.schedule import in_window, is_quiet, mode, next_delay

from .conftest import KYIV


def at(h, m, s=0):
    return datetime(2026, 11, 11, h, m, s, tzinfo=KYIV)


@pytest.mark.parametrize(
    "t, expected",
    [
        (time(0, 0), "night"), (time(3, 0), "night"), (time(5, 29), "night"),
        (time(5, 30), "normal"), (time(5, 57), "normal"),
        (time(5, 58), "rush"), (time(6, 0), "rush"), (time(6, 19), "rush"),
        (time(6, 20), "normal"), (time(12, 0), "normal"), (time(23, 59), "normal"),
    ],
)
def test_mode(settings, t, expected):
    assert mode(t, settings.model_copy(update={"rush_enabled": True})) == expected


def test_rush_disabled_by_default(settings):
    assert mode(time(6, 0), settings) == "normal"


def test_night_can_be_disabled(settings):
    s = settings.model_copy(update={"night_enabled": False})
    assert mode(time(3, 0), s) == "normal"


def test_window_over_midnight():
    assert in_window(time(23, 30), time(23, 0), time(6, 0))
    assert in_window(time(2, 0), time(23, 0), time(6, 0))
    assert not in_window(time(12, 0), time(23, 0), time(6, 0))


def test_normal_interval_with_jitter(settings):
    rng = random.Random(1)
    base = settings.interval_seconds
    delays = [next_delay(at(12, 0), settings, rng) for _ in range(200)]
    assert min(delays) >= base * 0.7 and max(delays) <= base * 1.3
    assert max(delays) - min(delays) > base * 0.2  # jitter is really there


def test_rush_interval_about_60s(settings):
    rng = random.Random(1)
    delays = [next_delay(at(6, 5), settings.model_copy(update={"rush_enabled": True}), rng) for _ in range(100)]
    assert min(delays) >= 54 and max(delays) <= 66


def test_night_interval_about_20min(settings):
    rng = random.Random(1)
    delays = [next_delay(at(2, 0), settings, rng) for _ in range(100)]
    assert min(delays) >= 1200 * 0.7 and max(delays) <= 1200 * 1.3


def test_does_not_oversleep_rush_start(settings):
    rng = random.Random(1)
    settings = settings.model_copy(update={"rush_enabled": True})
    assert next_delay(at(5, 20), settings, rng) <= 38 * 60  # night cycle capped to 05:58
    assert next_delay(at(5, 56), settings, rng) <= 120  # does not wake up after the window start

    big = settings.model_copy(update={"interval_seconds": 600})
    assert next_delay(at(5, 56), big, rng) == pytest.approx(120)  # a long interval is capped


def test_quiet_hours(settings):
    assert is_quiet(time(0, 0), settings)
    assert is_quiet(time(5, 59), settings)
    assert not is_quiet(time(6, 0), settings)
    assert not is_quiet(time(14, 0), settings)
    assert not is_quiet(time(3, 0), settings.model_copy(update={"quiet_enabled": False}))
