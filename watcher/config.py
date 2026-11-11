from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_OPTIONAL = (
    "telegram_bot_token", "telegram_chat_id", "date_from", "date_to",
    "browser_channel", "user_agent",
    "smtp_host", "smtp_user", "smtp_password", "email_to",
)


class ServiceCenter(BaseModel):
    id: int
    name: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- secrets ---
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: int | None = None

    # --- site (values from docs/findings.md) ---
    base_url: str = "https://eqn.hsc.gov.ua"
    public_url: str = "https://eq.hsc.gov.ua"
    service_id: int = 8  # "Theory exam"
    # The center name below is user-facing (it appears in the Telegram message),
    # so it is kept in Ukrainian on purpose.
    service_centers: list[ServiceCenter] = [ServiceCenter(id=142, name="Чернігів")]

    # --- which slots are interesting ---
    latest_time: time = time(10, 30)
    latest_inclusive: bool = True
    date_from: date | None = None
    date_to: date | None = None
    min_lead_minutes: int = 120
    timezone: str = "Europe/Kyiv"

    # --- schedule ---
    # The site session lives only 15 min (windowed mode): while the login is
    # valid, cycles should run almost back to back to fit more passes into a
    # window. One full pass of 15 dates at a 20-30 s pause takes ~7 min, hence a
    # short gap between cycles.
    interval_seconds: int = 60
    interval_jitter: float = 0.3
    # Window of frequent checks for a new edge date. Disabled: a new date opens
    # at 06:00 always free, so watching it makes no sense (user's decision).
    rush_enabled: bool = False
    rush_start: time = time(5, 58)
    rush_end: time = time(6, 20)
    rush_interval_seconds: int = 60
    night_enabled: bool = True
    night_start: time = time(0, 0)
    night_end: time = time(5, 30)
    night_interval_seconds: int = 1200
    # Which dates to read per cycle (the site rate-limits requests, findings §7):
    #   full   -- every date each cycle;
    #   tiered -- the nearest near_dates each cycle, the rest far_per_cycle per cycle.
    # In the rush window only edge_dates edge dates and just-opened ones are read.
    scan_mode: Literal["full", "tiered"] = "full"
    near_dates: int = 6
    far_per_cycle: int = 2
    edge_dates: int = 1
    # Whether to notify about slots on a just-opened edge date (and read it out of turn).
    notify_new_dates: bool = False
    # Site rate limit (findings §7): at a 7-9 s pause we hit 429 by the ~10th
    # request, while at 20-30 s a full 15-date pass went through with no refusals.
    request_pause_min: float = 20.0
    request_pause_max: float = 30.0

    # --- notifications ---
    quiet_enabled: bool = True
    quiet_start: time = time(0, 0)
    quiet_end: time = time(6, 0)
    heartbeat_time: time = time(9, 0)
    service_repeat_hours: float = 4.0
    stale_minutes: int = 30

    # --- errors ---
    block_pause_minutes: int = 60
    backoff_base_seconds: int = 30
    backoff_max_seconds: int = 1800

    # --- files ---
    db_path: Path = Path("data/watcher.sqlite")
    storage_state_path: Path = Path("data/storage_state.json")
    log_dir: Path = Path("logs")

    # --- browser (the same for login and for the monitor) ---
    browser_channel: str | None = "chrome"  # empty = Playwright's bundled Chromium
    browser_headless: bool = False  # headless is blocked by the site (Akamai), see findings
    browser_start_minimized: bool = True
    user_agent: str | None = None
    locale: str = "uk-UA"

    # --- fallback channel: email (off until SMTP_* are set) ---
    smtp_host: str | None = None
    smtp_port: int = 465
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    email_to: str | None = None

    @field_validator(*_OPTIONAL, mode="before")
    @classmethod
    def _empty_to_none(cls, v):
        return None if isinstance(v, str) and v.strip() == "" else v

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def now(self) -> datetime:
        return datetime.now(self.tz)

    @property
    def center_names(self) -> dict[int, str]:
        return {c.id: c.name for c in self.service_centers}

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_password and self.email_to)
