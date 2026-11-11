"""Parsing of the e-queue JSON API responses (formats -- docs/findings.md)."""
from __future__ import annotations

import json
from datetime import date, datetime, time

from watcher.errors import Blocked, RateLimited, SessionExpired, TransientError
from watcher.models import RawResponse, Slot


def departments_path(service_id: int) -> str:
    return f"/api/v2/equeue/departments?serviceId={service_id}"


def days_path(service_id: int, center_id: int) -> str:
    return f"/api/v2/equeue/days?serviceId={service_id}&departmentId={center_id}"


def slots_path(service_id: int, center_id: int, raw_date: str) -> str:
    return (
        f"/api/v2/equeue/slots?serviceId={service_id}"
        f"&departmentId={center_id}&date={raw_date}"
    )


def decode(resp: RawResponse) -> list:
    """Return the `data` payload or raise the matching error type.

    A valid response is only 200 + a JSON object with a list under `data`.
    An empty list is an honest "no slots"; anything else is an error after
    which the snapshot is NOT updated.
    """
    head = resp.text[:2000].lower()
    if resp.status == 401:
        raise SessionExpired("401 Unauthorized")
    if resp.status == 429:
        raise RateLimited("429 Too Many Requests")
    if resp.status == 403 or "captcha" in head or "access denied" in head:
        raise Blocked(f"HTTP {resp.status}: access blocked by the site (anti-bot/captcha)")
    if resp.status != 200:
        raise TransientError(f"HTTP {resp.status}")
    try:
        payload = json.loads(resp.text)
    except ValueError as exc:
        raise TransientError("response is not JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise TransientError("no data list in the response")
    return payload["data"]


def parse_days(resp: RawResponse) -> list[tuple[date, str]]:
    """List of (date, raw date string -- exactly what the site expects in `date`)."""
    out: list[tuple[date, str]] = []
    for item in decode(resp):
        try:
            raw = item["date"]
            out.append((datetime.fromisoformat(raw).date(), raw))
        except (KeyError, TypeError, ValueError) as exc:
            raise TransientError(f"unparseable days item: {item!r}") from exc
    return out


def parse_slots(resp: RawResponse, center_id: int, day: date) -> set[Slot]:
    out: set[Slot] = set()
    for item in decode(resp):
        try:
            out.add(Slot(center_id, day, time.fromisoformat(item["startTime"])))
        except (KeyError, TypeError, ValueError) as exc:
            raise TransientError(f"unparseable slots item: {item!r}") from exc
    return out
