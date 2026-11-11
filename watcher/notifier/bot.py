"""Telegram bot commands. They change nothing on the MVS site -- they only read SQLite.

The reply texts are sent to the user in Telegram, so they are kept in Ukrainian.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Callable

import httpx

from watcher.config import Settings
from watcher.core.filters import interesting
from watcher.models import Slot
from watcher.notifier.format import fmt_dt, slot_lines
from watcher.notifier.telegram import API
from watcher.storage.db import Database

log = logging.getLogger(__name__)


class BotCommands:
    def __init__(
        self,
        token: str,
        chat_id: int,
        db: Database,
        settings: Settings,
        now_fn: Callable[[], datetime] | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._token = token
        self.chat_id = chat_id
        self.db = db
        self.s = settings
        self.now_fn = now_fn or settings.now
        self._client = client or httpx.Client(timeout=40)

    # --- logic (tested without the network) ---
    def handle_update(self, update: dict) -> str | None:
        """Reply to an update or None. Other chats are ignored silently."""
        msg = update.get("message") or {}
        if (msg.get("chat") or {}).get("id") != self.chat_id:
            return None
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return None
        command = text.split()[0].split("@")[0].lower()
        handler = {
            "/status": self._status,
            "/pause": self._pause,
            "/resume": self._resume,
            "/slots": self._slots,
            "/start": self._help,
            "/help": self._help,
        }.get(command)
        return handler() if handler else None

    def _help(self) -> str:
        return (
            "Я стежу за слотами на теоретичний іспит і лише сповіщаю.\n"
            "/status — стан монітора\n/slots — поточні ранкові слоти\n"
            "/pause — вимкнути сповіщення\n/resume — увімкнути"
        )

    def _status(self) -> str:
        session = self.db.kv_get("session_ok")
        session_text = {"1": "працює", "0": "потрібен повторний вхід"}.get(session, "ще не перевірялась")
        paused = self.db.kv_get("paused") == "1"
        return (
            f"Останній успішний цикл: {fmt_dt(self.db.kv_get('last_success'))}\n"
            f"Сесія: {session_text}\n"
            f"Сповіщення: {'на паузі' if paused else 'увімкнено'}"
        )

    def _pause(self) -> str:
        self.db.kv_set("paused", "1")
        return "⏸ Сповіщення про слоти вимкнено. Увімкнути: /resume"

    def _resume(self) -> str:
        self.db.kv_set("paused", "0")
        return "▶️ Сповіщення про слоти увімкнено."

    def _slots(self) -> str:
        now = self.now_fn()
        snapshot: set[Slot] = set()
        for center in self.s.service_centers:
            snapshot |= self.db.get_snapshot(center.id) or set()
        slots = interesting(snapshot, now, self.s)
        stamp = fmt_dt(self.db.kv_get("last_success"))
        if not slots:
            return f"Вільних слотів до {self.s.latest_time:%H:%M} немає (знімок: {stamp})."
        lines = slot_lines(slots, self.s, now.date())
        return "\n".join([f"Вільні слоти до {self.s.latest_time:%H:%M} (знімок: {stamp}):", *lines])

    # --- network ---
    def _reply(self, text: str) -> None:
        self._client.post(
            f"{API}/bot{self._token}/sendMessage",
            json={"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True},
        )

    def poll_forever(self, stop: threading.Event) -> None:
        url = f"{API}/bot{self._token}/getUpdates"
        offset: int | None = None
        first = True  # the first request only discards old messages
        while not stop.is_set():
            try:
                if first:
                    params = {"offset": -1, "timeout": 0}
                else:
                    params = {"timeout": 25, "allowed_updates": '["message"]'}
                    if offset is not None:
                        params["offset"] = offset
                data = self._client.get(url, params=params).json()
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    if first:
                        continue
                    reply = self.handle_update(update)
                    if reply:
                        self._reply(reply)
                first = False
            except Exception as exc:  # the bot thread must not bring the monitor down
                log.warning("bot: %s", str(exc).replace(self._token, "***"))
                stop.wait(15)
