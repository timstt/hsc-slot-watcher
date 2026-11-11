from __future__ import annotations

import smtplib
from email.message import EmailMessage

from watcher.errors import NotifyError
from watcher.notifier.base import Notifier


class EmailNotifier(Notifier):
    """Fallback channel. For Gmail an App Password is required, not the main password."""

    def __init__(self, host: str, port: int, user: str, password: str, to: str) -> None:
        self.host, self.port, self.user, self._password, self.to = host, port, user, password, to

    def send(self, text: str, silent: bool = False) -> None:
        msg = EmailMessage()
        msg["Subject"] = text.splitlines()[0][:120] if text else "hsc-slot-watcher"
        msg["From"] = self.user
        msg["To"] = self.to
        msg.set_content(text)
        try:
            if self.port == 465:
                smtp = smtplib.SMTP_SSL(self.host, self.port, timeout=20)
            else:
                smtp = smtplib.SMTP(self.host, self.port, timeout=20)
                smtp.starttls()
            with smtp:
                smtp.login(self.user, self._password)
                smtp.send_message(msg)
        except (OSError, smtplib.SMTPException) as exc:
            raise NotifyError(f"email unavailable: {type(exc).__name__}") from exc
