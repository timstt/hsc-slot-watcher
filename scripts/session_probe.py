"""One-off check: whether the site renews the session near the end of its 15 minutes.

Keeps one window open (like the monitor) and makes a single read GET request
~60 s before the session cookie expires, right after it, and again 2 min later.
"""
import time
from datetime import datetime, timedelta

from watcher.browser.client import BrowserClient
from watcher.config import Settings

PATH = "/api/v2/equeue/departments?serviceId=8"


def main() -> None:
    client = BrowserClient(Settings())
    try:
        client._ensure()
        expiry = client.session_expiry()
        print(f"[{datetime.now():%H:%M:%S}] cookie valid until {expiry}", flush=True)
        for label, when in (
            ("60 s before expiry", expiry - timedelta(seconds=60)),
            ("20 s after expiry", expiry + timedelta(seconds=20)),
            ("2 min after expiry", expiry + timedelta(seconds=120)),
        ):
            time.sleep(max(0.0, (when - datetime.now()).total_seconds()))
            resp = client.get(PATH)
            print(f"[{datetime.now():%H:%M:%S}] {label}: HTTP {resp.status}, "
                  f"cookie valid until {client.session_expiry()}", flush=True)
        client.save_session()
    finally:
        client.close()


if __name__ == "__main__":
    main()
