from datetime import date, time

import pytest

from watcher.errors import Blocked, RateLimited, SessionExpired, TransientError
from watcher.models import RawResponse
from watcher.parser.api import days_path, decode, parse_days, parse_slots, slots_path

from .conftest import D1, fixture_response


def test_parse_days_real_fixture():
    days = parse_days(fixture_response("days.json"))
    assert len(days) == 15
    assert days[0] == (date(2026, 11, 12), "2026-11-12T00:00:00")
    assert days[-1][0] == date(2026, 12, 2)


def test_parse_slots_real_fixture():
    slots = parse_slots(fixture_response("slots_2026-11-12.json"), 142, D1)
    assert len(slots) == 20
    assert min(slots).start == time(11, 0)
    assert max(slots).start == time(15, 45)
    assert all(s.center_id == 142 and s.day == D1 for s in slots)


def test_empty_data_is_valid_no_slots():
    assert parse_slots(RawResponse(200, "application/json", '{"data":[]}'), 142, D1) == set()


def test_paths_match_findings():
    assert days_path(8, 142) == "/api/v2/equeue/days?serviceId=8&departmentId=142"
    assert slots_path(8, 142, "2026-11-12T00:00:00") == (
        "/api/v2/equeue/slots?serviceId=8&departmentId=142&date=2026-11-12T00:00:00"
    )


def test_expired_session_detected():
    with pytest.raises(SessionExpired):
        decode(fixture_response("unauthorized_401.json", status=401))


def test_akamai_block_detected():
    with pytest.raises(Blocked):
        decode(fixture_response("akamai_403.html", status=403, ctype="text/html"))


def test_captcha_page_with_200_is_block():
    with pytest.raises(Blocked):
        decode(RawResponse(200, "text/html", "<html>Please solve the CAPTCHA</html>"))


def test_rate_limit_detected():
    with pytest.raises(RateLimited):
        decode(RawResponse(429, "text/plain", "Too Many Requests"))


@pytest.mark.parametrize(
    "resp",
    [
        RawResponse(500, "text/html", "Internal Server Error"),
        RawResponse(502, "text/html", ""),
        RawResponse(200, "application/json", ""),
        RawResponse(200, "text/html", "<html>login</html>"),
        RawResponse(200, "application/json", "[]"),
        RawResponse(200, "application/json", '{"data":null}'),
        RawResponse(200, "application/json", '{"error":"x"}'),
    ],
)
def test_broken_responses_are_errors_not_empty(resp):
    with pytest.raises(TransientError):
        decode(resp)


def test_malformed_slot_item_is_error():
    with pytest.raises(TransientError):
        parse_slots(RawResponse(200, "application/json", '{"data":[{"startTime":"not-a-time"}]}'), 142, D1)
