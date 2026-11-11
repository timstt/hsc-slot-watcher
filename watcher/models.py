from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, tzinfo


@dataclass(frozen=True, order=True)
class Slot:
    center_id: int
    day: date
    start: time

    def starts_at(self, tz: tzinfo) -> datetime:
        return datetime.combine(self.day, self.start, tzinfo=tz)


@dataclass(frozen=True)
class RawResponse:
    """A raw site response -- everything the parser needs."""

    status: int
    content_type: str
    text: str


class SlotClient:
    """Source of site responses. Real one is the browser; in tests/simulate a fake."""

    warmed: bool = False  # whether the "pick a center" step ran in this session (see core/cycle.py)

    def get(self, path: str) -> RawResponse:  # pragma: no cover - interface
        raise NotImplementedError

    def save_session(self) -> None:
        """Persist refreshed cookies after a successful cycle."""

    def session_updated(self) -> bool:
        """Whether a new session file appeared (the user ran login again)."""
        return False

    def reload_session(self) -> None:
        """Re-read the session file."""

    def close(self) -> None:
        pass
