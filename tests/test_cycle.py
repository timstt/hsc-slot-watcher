from datetime import datetime, timedelta

import pytest

from watcher import simulate
from watcher.config import ServiceCenter
from watcher.core.cycle import run_cycle
from watcher.errors import NotifyError, TransientError
from watcher.notifier.base import Notifier, RecordingNotifier
from watcher.scheduler.runner import Monitor
from watcher.simulate import BLOCKED, BROKEN, EMPTY_BODY, EXPIRED, FakeClient

from .conftest import D1, D2, KYIV, NOW, slot

LATE = {D1: ["11:00", "11:15"], D2: ["12:00"]}


def cycle(client, db, settings, notifier, now=NOW):
    return run_cycle(client, db, notifier, settings, now)


def test_first_run_sends_current_morning_slots_once(db, settings):
    rec = RecordingNotifier()
    client = FakeClient({D1: ["09:00", "11:00"], D2: ["10:30", "10:45"]})
    cycle(client, db, settings, rec)
    assert len(rec.sent) == 1
    text = rec.sent[0][0]
    assert "Зараз доступні" in text and "09:00" in text and "10:30" in text
    assert "11:00" not in text and "10:45" not in text
    cycle(client, db, settings, rec)  # nothing changed -- silence
    assert len(rec.sent) == 1


def test_first_run_without_morning_slots_is_silent(db, settings):
    rec = RecordingNotifier()
    cycle(FakeClient(LATE), db, settings, rec)
    assert rec.sent == []
    assert db.get_snapshot(142) == {slot(D1, "11:00"), slot(D1, "11:15"), slot(D2, "12:00")}


def test_new_slot_notifies_and_late_slot_does_not(db, settings):
    rec = RecordingNotifier()
    client = FakeClient(LATE)
    cycle(client, db, settings, rec)
    client.state = {**LATE, D1: ["09:00", "11:00", "11:15", "13:00"]}
    cycle(client, db, settings, rec)
    assert len(rec.sent) == 1
    assert "09:00" in rec.sent[0][0] and "13:00" not in rec.sent[0][0]


def test_reappeared_slot_notifies_again(db, settings):
    rec = RecordingNotifier()
    client = FakeClient(LATE)
    cycle(client, db, settings, rec)
    with_slot = {**LATE, D1: ["09:00", "11:00", "11:15"]}
    for state in (with_slot, LATE, with_slot):
        client.state = state
        cycle(client, db, settings, rec)
    assert len(rec.sent) == 2


@pytest.mark.parametrize("failure", [BROKEN, EMPTY_BODY])
def test_broken_response_keeps_snapshot(db, settings, failure):
    rec = RecordingNotifier()
    good = {**LATE, D1: ["09:00", "11:00", "11:15"]}
    client = FakeClient(good)
    cycle(client, db, settings, rec)
    before = db.get_snapshot(142)
    rec.sent.clear()

    client.state = failure
    with pytest.raises(TransientError):
        cycle(client, db, settings, rec)
    assert db.get_snapshot(142) == before

    client.state = good
    cycle(client, db, settings, rec)
    assert rec.sent == []  # the slot did not "reappear" because of the failure


def test_failure_in_the_middle_of_dates_keeps_snapshot(db, settings):
    class FailsOnSecondDate(FakeClient):
        def get(self, path):
            if "date=2026-11-13" in path:
                self.state_backup, self.state = self.state, BROKEN
                try:
                    return super().get(path)
                finally:
                    self.state = self.state_backup
            return super().get(path)

    good = {D1: ["11:00"], D2: ["09:00"]}
    cycle(FakeClient(good), db, settings, None)
    before = db.get_snapshot(142)
    with pytest.raises(TransientError):
        cycle(FailsOnSecondDate(good), db, settings, None)
    assert db.get_snapshot(142) == before


def test_failed_notification_does_not_store_snapshot(db, settings):
    class Down(Notifier):
        def send(self, text, silent=False):
            raise NotifyError("down")

    client = FakeClient(LATE)
    cycle(client, db, settings, RecordingNotifier())
    client.state = {**LATE, D1: ["09:00", "11:00", "11:15"]}
    with pytest.raises(NotifyError):
        cycle(client, db, settings, Down())
    rec = RecordingNotifier()
    cycle(client, db, settings, rec)  # the next cycle delivers the same thing
    assert len(rec.sent) == 1 and "09:00" in rec.sent[0][0]


def test_pause_suppresses_slot_notifications(db, settings):
    rec = RecordingNotifier()
    client = FakeClient(LATE)
    cycle(client, db, settings, rec)
    db.kv_set("paused", "1")
    client.state = {**LATE, D1: ["09:00", "11:00", "11:15"]}
    cycle(client, db, settings, rec)
    assert rec.sent == []


def test_quiet_hours_send_silently(db, settings):
    rec = RecordingNotifier()
    client = FakeClient(LATE)
    night = datetime(2026, 11, 11, 3, 0, tzinfo=KYIV)
    cycle(client, db, settings, rec, night)
    client.state = {**LATE, D1: ["09:00", "11:00", "11:15"]}
    cycle(client, db, settings, rec, night)
    assert rec.sent[0][1] is True  # disable_notification

    client.state = {**LATE, D1: ["09:00", "09:15", "11:00", "11:15"]}
    cycle(client, db, settings, rec, NOW)
    assert rec.sent[1][1] is False


def test_requests_are_sequential_one_per_date(db, settings):
    client = FakeClient(LATE)
    pauses = []
    run_cycle(client, db, None, settings, NOW, pause=lambda: pauses.append(1))
    kinds = [p.split("/equeue/")[1].split("?")[0] for p in client.requests]
    assert kinds == ["departments", "days", "slots", "slots"]
    assert len(pauses) == 3  # a pause between all requests


class NeedsCenterStep(FakeClient):
    """Like the real site: days returns 204 until the center list has been requested."""

    site_warm = False

    def get(self, path):
        from watcher.models import RawResponse

        if "/equeue/departments" in path:
            self.requests.append(path)
            self.site_warm = True
            return RawResponse(200, "application/json", '{"data":[{"id":142}]}')
        if "/equeue/days" in path and not self.site_warm:
            self.requests.append(path)
            return RawResponse(204, "", "")
        return super().get(path)


def test_days_204_triggers_center_step_once(db, settings):
    client = NeedsCenterStep({D1: ["09:00"]})
    rec = RecordingNotifier()
    cycle(client, db, settings, rec)
    kinds = [p.split("/equeue/")[1].split("?")[0] for p in client.requests]
    assert kinds == ["departments", "days", "slots"]
    assert len(rec.sent) == 1
    client.requests.clear()
    cycle(client, db, settings, rec)
    assert [p.split("/equeue/")[1].split("?")[0] for p in client.requests] == ["days", "slots"]
    client.site_warm = False  # the site "forgot" the step although the client thinks it ran
    client.requests.clear()
    cycle(client, db, settings, rec)
    assert [p.split("/equeue/")[1].split("?")[0] for p in client.requests] == [
        "days", "departments", "days", "slots"]


def test_days_204_twice_is_error_not_empty(db, settings):
    class Always204(NeedsCenterStep):
        def get(self, path):
            result = super().get(path)
            self.site_warm = False
            return result

    cycle(FakeClient({D1: ["11:00"]}), db, settings, None)
    before = db.get_snapshot(142)
    with pytest.raises(TransientError):
        cycle(Always204({D1: ["11:00"]}), db, settings, None)
    assert db.get_snapshot(142) == before


def test_multiple_centers_named_in_message(db, settings):
    s = settings.model_copy(update={"service_centers": [
        ServiceCenter(id=142, name="Чернігів"), ServiceCenter(id=143, name="Прилуки")]})
    rec = RecordingNotifier()
    run_cycle(FakeClient({D1: ["09:00"]}), db, rec, s, NOW)
    text = "\n".join(t for t, _ in rec.sent)
    assert "Чернігів" in text and "Прилуки" in text


def test_stats_recorded_only_for_morning_slots_after_first_run(db, settings):
    client = FakeClient({D1: ["09:00"]})
    cycle(client, db, settings, RecordingNotifier())
    assert db.appearance_times() == []  # the first run is not an "appearance"
    client.state = {D1: ["09:00", "09:15", "13:00"]}
    cycle(client, db, settings, RecordingNotifier())
    assert len(db.appearance_times()) == 1


# --- Monitor: error handling and service messages ---
def make_monitor(db, settings, client, clock):
    rec = RecordingNotifier()
    return Monitor(client, db, rec, settings, now_fn=lambda: clock[0]), rec


def test_expired_session_one_message_then_pause(db, settings):
    clock = [NOW]
    client = FakeClient(EXPIRED)
    monitor, rec = make_monitor(db, settings, client, clock)
    monitor.step()
    assert len(rec.sent) == 1 and "python -m watcher login" in rec.sent[0][0]
    assert db.kv_get("session_ok") == "0"
    requests_before = len(client.requests)
    monitor.step()
    assert len(rec.sent) == 1  # no duplication
    assert len(client.requests) == requests_before  # while paused we do not hit the site
    clock[0] = NOW + timedelta(hours=5)
    monitor.step()
    assert len(rec.sent) == 2  # a reminder after a few hours


def test_resumes_after_new_session_file(db, settings):
    class Relogin(FakeClient):
        updated = False

        def session_updated(self):
            return self.updated

        def reload_session(self):
            self.state = LATE

    clock = [NOW]
    client = Relogin(EXPIRED)
    monitor, _ = make_monitor(db, settings, client, clock)
    monitor.step()
    assert monitor.need_login
    client.updated = True
    monitor.step()
    assert not monitor.need_login and db.kv_get("session_ok") == "1"


def test_block_long_pause_and_message(db, settings):
    clock = [NOW]
    monitor, rec = make_monitor(db, settings, FakeClient(BLOCKED), clock)
    delay = monitor.step()
    assert delay == settings.block_pause_minutes * 60
    assert len(rec.sent) == 1 and "блокує" in rec.sent[0][0]
    monitor.step()
    assert len(rec.sent) == 1


def test_transient_errors_exponential_backoff(db, settings):
    clock = [NOW]
    monitor, rec = make_monitor(db, settings, FakeClient(BROKEN), clock)
    delays = [monitor.step() for _ in range(8)]
    assert delays[:4] == [30, 60, 120, 240]
    assert delays[-1] == settings.backoff_max_seconds
    assert rec.sent == []  # 30 min have not passed yet


def test_stale_alert_after_30_minutes_without_success(db, settings):
    clock = [NOW]
    client = FakeClient(LATE)
    monitor, rec = make_monitor(db, settings, client, clock)
    monitor.step()
    client.state = BROKEN
    clock[0] = NOW + timedelta(minutes=31)
    monitor.step()
    assert len(rec.sent) == 1 and "Немає успішних циклів" in rec.sent[0][0]
    clock[0] = NOW + timedelta(minutes=40)
    monitor.step()
    assert len(rec.sent) == 1


def test_heartbeat_once_a_day(db, settings):
    from datetime import time

    s = settings.model_copy(update={"heartbeat_time": time(9, 0)})
    clock = [datetime(2026, 11, 11, 8, 50, tzinfo=KYIV)]
    monitor, rec = make_monitor(db, s, FakeClient(LATE), clock)
    monitor.step()
    assert rec.sent == []
    clock[0] = datetime(2026, 11, 11, 9, 1, tzinfo=KYIV)
    monitor.step()
    monitor.step()
    assert len(rec.sent) == 1
    assert "Монітор живий" in rec.sent[0][0] and "не було" in rec.sent[0][0]
    assert rec.sent[0][1] is True
    clock[0] = datetime(2026, 11, 12, 9, 1, tzinfo=KYIV)
    monitor.step()
    assert len(rec.sent) == 2


def test_simulate_all_scenarios_pass(settings, capsys):
    assert simulate.run(settings, RecordingNotifier()) is True
