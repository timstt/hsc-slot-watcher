from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from watcher.errors import NotifyError

log = logging.getLogger(__name__)


class Notifier(ABC):
    @abstractmethod
    def send(self, text: str, silent: bool = False) -> None:
        """Deliver a message or raise NotifyError."""


class ConsoleNotifier(Notifier):
    """Prints instead of sending (--dry-run)."""

    def send(self, text: str, silent: bool = False) -> None:
        mark = "silent" if silent else "with sound"
        print(f"--- message ({mark}) ---\n{text}\n---")


class RecordingNotifier(Notifier):
    """Remembers what was sent and forwards it on (simulate, tests)."""

    def __init__(self, inner: Notifier | None = None) -> None:
        self.inner = inner
        self.sent: list[tuple[str, bool]] = []

    def send(self, text: str, silent: bool = False) -> None:
        if self.inner is not None:
            self.inner.send(text, silent)
        self.sent.append((text, silent))


class FallbackNotifier(Notifier):
    """Primary channel, and if it is unavailable -- the fallback (when configured)."""

    def __init__(self, primary: Notifier, secondary: Notifier | None = None) -> None:
        self.primary = primary
        self.secondary = secondary

    def send(self, text: str, silent: bool = False) -> None:
        try:
            self.primary.send(text, silent)
        except NotifyError as exc:
            if self.secondary is None:
                raise
            log.warning("primary channel unavailable (%s), trying the fallback", exc)
            self.secondary.send(text, silent)
