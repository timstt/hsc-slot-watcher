"""Error types the monitor uses to decide what to do next."""


class WatcherError(Exception):
    pass


class SessionExpired(WatcherError):
    """401 -- a manual re-login is required."""


class Blocked(WatcherError):
    """403 / captcha / anti-bot. We do not bypass it: stop and notify."""


class RateLimited(WatcherError):
    """429 -- too many requests. Long pause and notify."""


class TransientError(WatcherError):
    """Network, 5xx, broken response. The snapshot is not updated; back off."""


class NotifyError(WatcherError):
    """Could not deliver a notification through any channel."""


class ConfigError(WatcherError):
    pass
