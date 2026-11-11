import json
from datetime import date

import httpx
import pytest

from watcher.errors import NotifyError
from watcher.notifier.base import FallbackNotifier, Notifier, RecordingNotifier
from watcher.notifier.bot import BotCommands
from watcher.notifier.format import format_slots
from watcher.notifier.telegram import TelegramNotifier

from .conftest import D1, D2, NOW, slot

TOKEN = "123:TEST-TOKEN"
CHAT = 555


# --- formatting (asserted strings are the Ukrainian Telegram output) ---
def test_single_slot_message_exact_format(settings):
    text = format_slots([slot(D1, "09:00")], settings, NOW.date())
    assert text == (
        "🚗 Звільнився слот на теорію\n"
        "Завтра (чт, 12.11) о 09:00 — Чернігів\n"
        "https://eq.hsc.gov.ua"
    )


def test_many_slots_one_message_sorted(settings):
    slots = sorted([slot(date(2026, 11, 20), "08:00"), slot(D2, "10:30"), slot(D1, "09:15"), slot(D1, "09:00")])
    lines = format_slots(slots, settings, NOW.date()).splitlines()
    assert lines == [
        "🚗 Звільнились слоти на теорію",
        "Завтра (чт, 12.11): 09:00, 09:15 — Чернігів",
        "Післязавтра (пт, 13.11) о 10:30 — Чернігів",
        "Пт, 20.11 о 08:00 — Чернігів",
        "https://eq.hsc.gov.ua",
    ]


def test_today_label(settings):
    assert "Сьогодні (ср, 11.11)" in format_slots([slot(NOW.date(), "09:00")], settings, NOW.date())


# --- Telegram (network mocked) ---
def make_tg(handler, retries=4):
    sleeps = []
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return TelegramNotifier(TOKEN, CHAT, client=client, retries=retries, sleep=sleeps.append), sleeps


def test_telegram_payload_and_silent_flag():
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True})

    tg, _ = make_tg(handler)
    tg.send("привіт", silent=True)
    tg.send("зі звуком")
    path, body = seen[0]
    assert path == f"/bot{TOKEN}/sendMessage"
    assert body["chat_id"] == CHAT and body["text"] == "привіт"
    assert body["disable_notification"] is True
    assert seen[1][1]["disable_notification"] is False


def test_telegram_retries_with_backoff_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectError("no network")
        return httpx.Response(200, json={"ok": True})

    tg, sleeps = make_tg(handler)
    tg.send("x")
    assert len(calls) == 3 and sleeps == [2.0, 4.0]


def test_telegram_gives_up_without_leaking_token():
    tg, _ = make_tg(lambda r: httpx.Response(502, text=f"bad gateway {TOKEN}"), retries=2)
    with pytest.raises(NotifyError) as err:
        tg.send("x")
    assert TOKEN not in str(err.value)


def test_telegram_4xx_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(403, json={"ok": False, "description": "bot was blocked"})

    tg, _ = make_tg(handler)
    with pytest.raises(NotifyError):
        tg.send("x")
    assert len(calls) == 1


class Down(Notifier):
    def send(self, text, silent=False):
        raise NotifyError("down")


def test_fallback_to_email_when_telegram_down():
    backup = RecordingNotifier()
    FallbackNotifier(Down(), backup).send("слот")
    assert backup.sent == [("слот", False)]


def test_no_fallback_configured_raises():
    with pytest.raises(NotifyError):
        FallbackNotifier(Down(), None).send("слот")


def test_email_disabled_by_default(settings):
    assert settings.email_enabled is False


# --- bot commands (asserted replies are the Ukrainian Telegram output) ---
def upd(text, chat_id=CHAT):
    return {"update_id": 1, "message": {"chat": {"id": chat_id, "type": "private"}, "text": text}}


@pytest.fixture
def bot(db, settings):
    return BotCommands(TOKEN, CHAT, db, settings, now_fn=lambda: NOW)


def test_bot_ignores_other_chats(bot, db):
    assert bot.handle_update(upd("/status", chat_id=999)) is None
    assert bot.handle_update(upd("/pause", chat_id=999)) is None
    assert db.kv_get("paused") is None


def test_bot_ignores_plain_text_and_unknown(bot):
    assert bot.handle_update(upd("привіт")) is None
    assert bot.handle_update(upd("/book")) is None
    assert bot.handle_update({"update_id": 2}) is None


def test_bot_pause_resume(bot, db):
    assert "вимкнено" in bot.handle_update(upd("/pause"))
    assert db.kv_get("paused") == "1"
    assert "увімкнено" in bot.handle_update(upd("/resume"))
    assert db.kv_get("paused") == "0"


def test_bot_status(bot, db):
    assert "ще не було" in bot.handle_update(upd("/status"))
    db.kv_set("last_success", NOW.isoformat())
    db.kv_set("session_ok", "0")
    text = bot.handle_update(upd("/status"))
    assert "11.11 12:00" in text and "потрібен повторний вхід" in text


def test_bot_slots_shows_only_morning(bot, db):
    assert "немає" in bot.handle_update(upd("/slots"))
    db.replace_snapshot(142, {slot(D1, "09:00"), slot(D1, "10:30"), slot(D1, "10:45"), slot(D2, "14:00")})
    text = bot.handle_update(upd("/slots@my_bot"))
    assert "09:00, 10:30" in text
    assert "10:45" not in text and "14:00" not in text and "13.11" not in text
