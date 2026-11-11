from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from watcher.config import Settings
from watcher.models import RawResponse, Slot
from watcher.storage.db import Database

FIXTURES = Path(__file__).parent / "fixtures"
KYIV = ZoneInfo("Europe/Kyiv")
# Wednesday 2026-11-11, noon -- "now" for most tests.
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=KYIV)
D1 = date(2026, 11, 12)
D2 = date(2026, 11, 13)


def fixture_response(name: str, status: int = 200, ctype: str = "application/json") -> RawResponse:
    return RawResponse(status, ctype, (FIXTURES / name).read_text(encoding="utf-8"))


def slot(day: date, hhmm: str, center: int = 142) -> Slot:
    return Slot(center, day, time.fromisoformat(hhmm))


@pytest.fixture
def settings() -> Settings:
    # _env_file=None -- tests must not read the real .env
    return Settings(_env_file=None, heartbeat_time=time(23, 59, 59))


@pytest.fixture
def db() -> Database:
    return Database(":memory:")
