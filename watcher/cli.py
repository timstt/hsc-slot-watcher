from __future__ import annotations

import argparse
import logging
import random
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

from watcher.config import Settings
from watcher.errors import Blocked, ConfigError, NotifyError, SessionExpired, WatcherError

log = logging.getLogger("watcher")


class _Redact(logging.Filter):
    """Keeps secrets out of logs, even if they leaked into an error message."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self.secrets = [x for x in secrets if x]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for secret in self.secrets:
            msg = msg.replace(secret, "***")
        record.msg, record.args = msg, None
        return True


def setup_logging(s: Settings, to_file: bool) -> None:
    secrets = [x.get_secret_value() for x in (s.telegram_bot_token, s.smtp_password) if x]
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if to_file:
        s.log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(
            s.log_dir / "watcher.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"))
    for h in handlers:
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        h.addFilter(_Redact(secrets))
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs URLs that contain the token
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _pause_fn(s: Settings):
    rng = random.Random()
    return lambda: time.sleep(rng.uniform(s.request_pause_min, s.request_pause_max))


# ---------------------------------------------------------------- commands
def cmd_login(s: Settings, args) -> int:
    from watcher.browser.client import login

    return 0 if login(s) else 1


def cmd_run(s: Settings, args) -> int:
    from watcher.browser.client import BrowserClient
    from watcher.notifier.bot import BotCommands
    from watcher.notifier.factory import build_notifier
    from watcher.scheduler.runner import Monitor
    from watcher.storage.db import Database

    setup_logging(s, to_file=True)
    notifier = build_notifier(s)
    if not s.storage_state_path.exists():
        print("No session file. Run first: python -m watcher login")
        return 1
    db = Database(s.db_path)
    client = BrowserClient(s)
    stop = threading.Event()
    bot = BotCommands(s.telegram_bot_token.get_secret_value(), s.telegram_chat_id, db, s)
    threading.Thread(target=bot.poll_forever, args=(stop,), daemon=True, name="bot").start()
    monitor = Monitor(client, db, notifier, s, pause=_pause_fn(s))
    log.info("monitor started; centers: %s", ", ".join(c.name for c in s.service_centers))
    try:
        while True:
            delay = monitor.step()
            log.info("next cycle in %.0f s", delay)
            time.sleep(delay)
    except KeyboardInterrupt:
        log.info("stopped by the user")
    finally:
        stop.set()
        client.close()
        db.close()
    return 0


def cmd_once(s: Settings, args) -> int:
    """One real cycle: print to the console, no notifications and no write to the monitor DB."""
    from watcher.browser.client import BrowserClient
    from watcher.core.cycle import run_cycle
    from watcher.core.filters import is_interesting
    from watcher.notifier.format import day_label
    from watcher.storage.db import Database

    setup_logging(s, to_file=False)
    logging.getLogger("watcher").setLevel(logging.DEBUG)  # show every request
    if not s.storage_state_path.exists():
        print("No session file. Run first: python -m watcher login")
        return 1
    client = BrowserClient(s)
    now = s.now()

    def expiry(label: str) -> None:
        try:
            when = client.session_expiry()
            until = f"{when:%H:%M:%S}" if when else "unknown"
            print(f"[{datetime.now():%H:%M:%S}] session cookie valid until {until} ({label})")
        except Exception:
            pass

    try:
        client._ensure()
        expiry("before the cycle")
        results = run_cycle(client, Database(":memory:"), None, s, now, _pause_fn(s), full=True)
    except SessionExpired:
        print("Session expired. Run: python -m watcher login")
        return 2
    except Blocked as exc:
        print(f"Access blocked by the site: {exc}. Bypassing nothing.")
        return 3
    except WatcherError as exc:
        print(f"Cycle failed: {type(exc).__name__}: {exc}")
        return 4
    finally:
        expiry("after the cycle")
        try:
            client.save_session()  # the site refreshes the session cookie -- save it even after a failure
        except Exception:
            pass
        client.close()
    limit = f"{s.latest_time:%H:%M}"
    for res in results:
        print(f"\n{s.center_names[res.center_id]}: dates with slots "
              f"{len({x.day for x in res.snapshot})}, total slots {len(res.snapshot)}")
        for day in sorted({x.day for x in res.snapshot}):
            slots = sorted(x for x in res.snapshot if x.day == day)
            early = [f"{x.start:%H:%M}" for x in slots if is_interesting(x, now, s)]
            print(f"  {day_label(day, now.date()):<24} slots: {len(slots):>2} | "
                  f"<= {limit}: {', '.join(early) if early else '-'}")
    return 0


def cmd_simulate(s: Settings, args) -> int:
    from watcher import simulate
    from watcher.notifier.base import ConsoleNotifier
    from watcher.notifier.factory import build_notifier

    setup_logging(s, to_file=False)
    logging.getLogger().setLevel(logging.ERROR)
    notifier = ConsoleNotifier() if args.dry_run else build_notifier(s)
    ok = simulate.run(s, notifier)
    print("\nAll scenarios passed." if ok else "\nThere are mismatches -- see above.")
    return 0 if ok else 1


def cmd_test_notify(s: Settings, args) -> int:
    from watcher.notifier.factory import build_notifier

    setup_logging(s, to_file=False)
    # This text is delivered to Telegram, so it is kept in Ukrainian.
    build_notifier(s).send("✅ hsc-slot-watcher: тестове повідомлення. Сповіщення працюють.")
    print("Sent. Check Telegram.")
    return 0


def cmd_chat_id(s: Settings, args) -> int:
    import httpx

    if not s.telegram_bot_token:
        raise ConfigError("TELEGRAM_BOT_TOKEN is not set in .env.")
    token = s.telegram_bot_token.get_secret_value()
    data = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20).json()
    if not data.get("ok"):
        print(f"Telegram returned an error: {data.get('description')}")
        return 1
    chats = {}
    for upd in data.get("result", []):
        chat = (upd.get("message") or {}).get("chat") or {}
        if chat.get("type") == "private":
            chats[chat["id"]] = chat.get("first_name") or chat.get("username") or ""
    if not chats:
        print("No messages. Open the chat with the bot, press Start, and run this again.")
        return 1
    for cid, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={cid}   # {name}")
    return 0


def cmd_check_access(s: Settings, args) -> int:
    from watcher.browser.client import BrowserClient

    setup_logging(s, to_file=False)
    client = BrowserClient(s, use_session=False)
    try:
        status, title = client.home_status()
    except Exception as exc:
        print(f"UNAVAILABLE: the site did not open from this computer ({str(exc).splitlines()[0]}).")
        return 2
    finally:
        client.close()
    if status == 200:
        print(f"OK: {s.base_url} opens (HTTP 200, \"{title}\").")
        return 0
    if status in (403, 429):
        print(f"BLOCKED: HTTP {status} (\"{title}\"). The site does not allow this IP/browser. "
              "I will not bypass it -- use different hosting (in Ukraine) or a regular browser.")
        return 3
    print(f"UNAVAILABLE: HTTP {status} (\"{title}\").")
    return 2


def cmd_stats(s: Settings, args) -> int:
    from watcher.storage.db import Database

    times = Database(s.db_path).appearance_times()
    limit = f"{s.latest_time:%H:%M}"
    if not times:
        print(f"No data yet: no new slots <= {limit} have appeared.")
        return 0
    print(f"New slots <= {limit} recorded: {len(times)}")

    def table(title: str, counts: Counter, labels: list[tuple[int, str]]) -> None:
        print(f"\n{title}")
        top = max(counts.values())
        for key, label in labels:
            n = counts.get(key, 0)
            if n:
                print(f"  {label:<10} {n:>4}  {'#' * max(1, round(20 * n / top))}")

    table("By hour of day when the slot appeared:", Counter(t.hour for t in times),
          [(h, f"{h:02d}:00") for h in range(24)])
    table("By weekday:", Counter(t.weekday() for t in times), list(enumerate(WEEKDAYS_FULL_EN)))
    return 0


# English weekday labels for the stats table (console output, not Telegram).
WEEKDAYS_FULL_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def cmd_health(s: Settings, args) -> int:
    from watcher.storage.db import Database

    last = Database(s.db_path).kv_get("last_success")
    if last and s.now() - datetime.fromisoformat(last) <= timedelta(minutes=args.max_age):
        print(f"OK: last successful cycle {last}")
        return 0
    print(f"BAD: last successful cycle {last or 'none'}")
    return 1


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    parser = argparse.ArgumentParser(
        prog="python -m watcher",
        description="MVS e-queue monitor: notifies via Telegram only, books nothing.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="manual login via ID.GOV.UA and save the session")
    sub.add_parser("run", help="start the monitor")
    sub.add_parser("once", help="one real cycle, console output, no notifications")
    p = sub.add_parser("simulate", help="run the test scenarios on fake data")
    p.add_argument("--dry-run", action="store_true", help="print instead of sending to Telegram")
    sub.add_parser("test-notify", help="send a test message to Telegram")
    sub.add_parser("chat-id", help="show the chat_id of whoever messaged the bot")
    sub.add_parser("check-access", help="whether the site opens from this computer")
    sub.add_parser("stats", help="when morning slots appear most often")
    p = sub.add_parser("health", help="exit 0 if the last successful cycle is within N minutes")
    p.add_argument("--max-age", type=int, default=30, metavar="MIN")
    args = parser.parse_args(argv)

    commands = {
        "login": cmd_login, "run": cmd_run, "once": cmd_once, "simulate": cmd_simulate,
        "test-notify": cmd_test_notify, "chat-id": cmd_chat_id,
        "check-access": cmd_check_access, "stats": cmd_stats, "health": cmd_health,
    }
    try:
        return commands[args.command](Settings(), args)
    except (ConfigError, NotifyError) as exc:
        print(f"Error: {exc}")
        return 1
