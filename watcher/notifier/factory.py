from __future__ import annotations

from watcher.config import Settings
from watcher.errors import ConfigError
from watcher.notifier.base import FallbackNotifier, Notifier
from watcher.notifier.email import EmailNotifier
from watcher.notifier.telegram import TelegramNotifier


def build_telegram(s: Settings) -> TelegramNotifier:
    if not s.telegram_bot_token or s.telegram_chat_id is None:
        raise ConfigError(
            "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set in .env "
            "(find chat_id with `python -m watcher chat-id`)."
        )
    return TelegramNotifier(s.telegram_bot_token.get_secret_value(), s.telegram_chat_id)


def build_notifier(s: Settings) -> Notifier:
    email = None
    if s.email_enabled:
        email = EmailNotifier(
            s.smtp_host, s.smtp_port, s.smtp_user, s.smtp_password.get_secret_value(), s.email_to
        )
    return FallbackNotifier(build_telegram(s), email)
