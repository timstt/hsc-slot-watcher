from __future__ import annotations

import logging
import time
from typing import Callable

import httpx

from watcher.errors import NotifyError
from watcher.notifier.base import Notifier

log = logging.getLogger(__name__)
API = "https://api.telegram.org"


class TelegramNotifier(Notifier):
    """Writes through the Bot API only to one given chat_id."""

    def __init__(
        self,
        token: str,
        chat_id: int,
        client: httpx.Client | None = None,
        retries: int = 4,
        backoff: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._token = token
        self.chat_id = chat_id
        self._client = client or httpx.Client(timeout=20)
        self.retries = retries
        self.backoff = backoff
        self._sleep = sleep

    def _clean(self, text: object) -> str:
        return str(text).replace(self._token, "***")

    def call(self, method: str, **kwargs) -> httpx.Response:
        return self._client.post(f"{API}/bot{self._token}/{method}", **kwargs)

    def send(self, text: str, silent: bool = False) -> None:
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_notification": silent,
            "disable_web_page_preview": True,
        }
        last = "unknown error"
        for attempt in range(self.retries):
            if attempt:
                self._sleep(self.backoff * 2 ** (attempt - 1))
            try:
                resp = self.call("sendMessage", json=payload)
            except httpx.HTTPError as exc:
                last = self._clean(f"{type(exc).__name__}: {exc}")
                log.warning("Telegram unavailable (attempt %d): %s", attempt + 1, last)
                continue
            if resp.status_code == 200:
                return
            last = self._clean(f"HTTP {resp.status_code}: {resp.text[:200]}")
            if resp.status_code == 429 or resp.status_code >= 500:
                log.warning("Telegram temporarily refused (attempt %d): %s", attempt + 1, last)
                continue
            # 400/401/403 -- retrying will not help (bad token, chat_id, Start not pressed)
            raise NotifyError(f"Telegram rejected the message: {last}")
        raise NotifyError(f"Telegram unavailable after {self.retries} attempts: {last}")
