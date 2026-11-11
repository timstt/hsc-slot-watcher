"""The monitor loop: one step = one cycle + decision how long to wait next."""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta
from typing import Callable

from watcher.config import Settings
from watcher.core.cycle import CycleResult, run_cycle
from watcher.errors import Blocked, NotifyError, RateLimited, SessionExpired
from watcher.models import SlotClient
from watcher.notifier.base import Notifier
from watcher.scheduler.schedule import is_quiet, next_delay
from watcher.storage.db import Database

log = logging.getLogger(__name__)

# The texts below are sent to the user in Telegram, so they are kept in Ukrainian
# (that is the product's language). Log messages and comments are in English.
RELOGIN_TEXT = (
    "🔑 Сесія на сайті закінчилась (сайт тримає вхід лише 15 хв).\n"
    "Щоб монітор знову стежив, запусти `python -m watcher login` і увійди "
    "(найшвидше через Дія.Підпис). Монітор на паузі й сам продовжить, щойно "
    "з'явиться нова сесія — повторно про це не нагадуватиму кілька годин."
)
BLOCKED_TEXT = (
    "⛔ Сайт е-черги блокує доступ ({reason}).\n"
    "Я нічого не обходжу: монітор на паузі на {minutes} хв, потім спробує ще раз.\n"
    "Перевір вручну, чи відкривається сайт у браузері."
)
STALE_TEXT = "⚠️ Немає успішних циклів уже понад {minutes} хв. Остання помилка: {error}"
NEED_LOGIN_DELAY = 120.0


class Monitor:
    def __init__(
        self,
        client: SlotClient,
        db: Database,
        notifier: Notifier,
        settings: Settings,
        now_fn: Callable[[], datetime] | None = None,
        rng: random.Random | None = None,
        pause: Callable[[], None] | None = None,
        prefix: str = "",
    ) -> None:
        self.client = client
        self.db = db
        self.notifier = notifier
        self.s = settings
        self.now_fn = now_fn or settings.now
        self.rng = rng or random.Random()
        self.pause = pause or (lambda: None)
        self.prefix = prefix
        self.failures = 0
        self.need_login = False
        self.started = self.now_fn()
        self.last_results: list[CycleResult] = []

    def step(self) -> float:
        """Run one cycle. Returns the pause until the next one, in seconds."""
        now = self.now_fn()
        self.last_results = []
        delay = self._cycle(now)
        self._heartbeat(now)
        return delay

    def _cycle(self, now: datetime) -> float:
        if self.need_login:
            if not self.client.session_updated():
                self._service("relogin", RELOGIN_TEXT, now)
                return NEED_LOGIN_DELAY  # we do not hit the site
            log.info("new session file found, resuming")
            self.client.reload_session()
            self.need_login = False

        try:
            self.last_results = run_cycle(
                self.client, self.db, self.notifier, self.s, now, self.pause, self.prefix
            )
        except SessionExpired:
            log.warning("session expired (401)")
            self.need_login = True
            self.db.kv_set("session_ok", "0")
            self._service("relogin", RELOGIN_TEXT, now)
            return NEED_LOGIN_DELAY
        except (Blocked, RateLimited) as exc:
            self._save_session()
            log.error("blocked: %s", exc)
            minutes = self.s.block_pause_minutes
            self._service("blocked", BLOCKED_TEXT.format(reason=exc, minutes=minutes), now)
            return minutes * 60.0
        except Exception as exc:  # network, 5xx, broken response, notification failure
            self.failures += 1
            self._save_session()
            if isinstance(exc, NotifyError):
                log.error("failed to send notification: %s", exc)
            else:
                log.warning("cycle failed (%d in a row): %s: %s",
                            self.failures, type(exc).__name__, exc)
            self._check_stale(now, f"{type(exc).__name__}: {exc}")
            return float(min(
                self.s.backoff_base_seconds * 2 ** (self.failures - 1),
                self.s.backoff_max_seconds,
            ))

        self.failures = 0
        self._save_session()
        return next_delay(now, self.s, self.rng)

    def _save_session(self) -> None:
        """The site refreshes the session cookie during requests -- save it after each cycle."""
        try:
            self.client.save_session()
        except Exception as exc:
            log.warning("failed to save session: %s", exc)

    # --- service notifications ---
    def _service(self, key: str, text: str, now: datetime, silent: bool | None = None) -> bool:
        """Send a service notification unless one was sent in the last N hours."""
        if not self.db.service_due(key, now, self.s.service_repeat_hours):
            return False
        try:
            quiet = is_quiet(now.time(), self.s) if silent is None else silent
            self.notifier.send(self.prefix + text, silent=quiet)
        except NotifyError as exc:
            log.error("service message '%s' not delivered: %s", key, exc)
            return False
        self.db.service_mark(key, now)
        return True

    def _check_stale(self, now: datetime, error: str) -> None:
        last = self.db.kv_get("last_success")
        since = datetime.fromisoformat(last) if last else self.started
        if now - since > timedelta(minutes=self.s.stale_minutes):
            self._service("stale", STALE_TEXT.format(minutes=self.s.stale_minutes, error=error[:200]), now)

    def _heartbeat(self, now: datetime) -> None:
        today = now.date().isoformat()
        if now.time() < self.s.heartbeat_time or self.db.kv_get("heartbeat_day") == today:
            return
        last = self.db.kv_get("last_success")
        # Heartbeat text is a Telegram message -> Ukrainian.
        last_text = datetime.fromisoformat(last).strftime("%H:%M (%d.%m)") if last else "ще не було"
        found = int(self.db.kv_get("notified_since_hb", "0") or 0)
        limit = f"{self.s.latest_time:%H:%M}"
        tail = (f"нових слотів до {limit} не було" if not found
                else f"нових слотів до {limit} за добу: {found}")
        try:
            self.notifier.send(
                f"{self.prefix}💚 Монітор живий, останній успішний цикл о {last_text}, {tail}",
                silent=True,
            )
        except NotifyError as exc:
            log.error("heartbeat not delivered: %s", exc)
            return
        self.db.kv_set("heartbeat_day", today)
        self.db.kv_set("notified_since_hb", "0")
