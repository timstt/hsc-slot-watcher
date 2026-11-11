"""Browser client: a regular (headed) Chrome driven by Playwright.

Principles:
- login is MANUAL only (login() just waits for a human to sign in);
- no masking: no User-Agent spoofing by default, no stealth;
- 403/captcha -> Blocked exception, no bypass;
- read-only GET requests to the API.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from watcher.config import Settings
from watcher.errors import Blocked, TransientError
from watcher.models import RawResponse, SlotClient

log = logging.getLogger(__name__)

_FETCH_JS = """async (url) => {
  const r = await fetch(url, {
    method: 'GET',
    headers: {accept: 'application/json, text/plain, */*'},
    credentials: 'same-origin',
  });
  return {status: r.status, ctype: r.headers.get('content-type') || '', text: await r.text()};
}"""


def restrict(path: Path) -> None:
    """chmod 600. On Windows chmod is mostly a no-op -- the file lives in the user profile anyway."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _launch(pw, s: Settings, headless: bool | None = None, minimized: bool = False):
    headless = s.browser_headless if headless is None else headless
    args = ["--start-minimized"] if minimized and not headless else []
    return pw.chromium.launch(headless=headless, channel=s.browser_channel or None, args=args)


def _context_options(s: Settings) -> dict:
    opts: dict = {"locale": s.locale, "timezone_id": s.timezone}
    if s.user_agent:
        opts["user_agent"] = s.user_agent
    return opts


class BrowserClient(SlotClient):
    def __init__(self, settings: Settings, use_session: bool = True) -> None:
        self.s = settings
        self.use_session = use_session
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._home_ok = False
        self._mtime: float | None = None

    # --- lifecycle ---
    def _state_path(self) -> Path:
        return self.s.storage_state_path

    def _file_mtime(self) -> float | None:
        try:
            return self._state_path().stat().st_mtime
        except OSError:
            return None

    def _ensure(self) -> None:
        if self._page is None:
            from playwright.sync_api import sync_playwright

            if self._pw is None:
                self._pw = sync_playwright().start()
                self._browser = _launch(self._pw, self.s, minimized=self.s.browser_start_minimized)
            opts = _context_options(self.s)
            if self.use_session and self._state_path().exists():
                opts["storage_state"] = str(self._state_path())
            self._mtime = self._file_mtime()
            self._context = self._browser.new_context(**opts)
            self._page = self._context.new_page()
            self._home_ok = False
        if not self._home_ok:
            self._open_home()

    def _open_home(self) -> None:
        from playwright.sync_api import Error as PwError

        try:
            resp = self._page.goto(self.s.base_url, wait_until="domcontentloaded", timeout=45000)
        except PwError as exc:
            raise TransientError(f"site did not open: {str(exc).splitlines()[0]}") from exc
        status = resp.status if resp else 0
        if status in (403, 429):
            raise Blocked(f"HTTP {status} on the home page")
        if status >= 400 or status == 0:
            raise TransientError(f"home page: HTTP {status}")
        self._page.wait_for_timeout(1500)
        self._home_ok = True

    def home_status(self) -> tuple[int, str]:
        """For check-access: status and title of the home page."""
        from playwright.sync_api import sync_playwright

        if self._pw is None:
            self._pw = sync_playwright().start()
            self._browser = _launch(self._pw, self.s, minimized=False)
        ctx = self._browser.new_context(**_context_options(self.s))
        page = ctx.new_page()
        try:
            resp = page.goto(self.s.base_url, wait_until="domcontentloaded", timeout=45000)
            return (resp.status if resp else 0), page.title()
        finally:
            ctx.close()

    # --- SlotClient ---
    def get(self, path: str) -> RawResponse:
        from playwright.sync_api import Error as PwError

        self._ensure()
        try:
            data = self._page.evaluate(_FETCH_JS, path)
        except PwError as exc:
            self._home_ok = False  # reopen the page next time
            raise TransientError(f"request not performed: {str(exc).splitlines()[0]}") from exc
        return RawResponse(int(data["status"]), data["ctype"], data["text"])

    def save_session(self) -> None:
        """Refresh cookies in the session file (without context.storage_state(), which opens tabs)."""
        if not self.use_session or self._context is None:
            return
        path = self._state_path()
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {"origins": []}
        state["cookies"] = self._context.cookies()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        restrict(tmp)
        tmp.replace(path)
        self._mtime = self._file_mtime()

    def session_expiry(self):
        """When the site session cookie expires (None if unknown). Cookie values are not touched."""
        from datetime import datetime

        if self._context is None:
            return None
        times = [c["expires"] for c in self._context.cookies()
                 if c["name"] in ("__Secure-auth.access-token", "__Host-next.equeue-session")
                 and c.get("expires", -1) > 0]
        return datetime.fromtimestamp(min(times)) if times else None

    def session_updated(self) -> bool:
        current = self._file_mtime()
        return current is not None and current != self._mtime

    def reload_session(self) -> None:
        if self._context is not None:
            self._context.close()
        self._context = self._page = None
        self.warmed = False

    def close(self) -> None:
        for obj, method in ((self._context, "close"), (self._browser, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, method)()
            except Exception:
                pass
        self._context = self._page = self._browser = self._pw = None


def login(s: Settings, timeout_minutes: int = 15) -> bool:
    """Open a window, wait for a MANUAL login, save the session. Clicks nothing itself."""
    from playwright.sync_api import TimeoutError as PwTimeout
    from playwright.sync_api import sync_playwright

    path = s.storage_state_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = _launch(pw, s, headless=False)
        context = browser.new_context(**_context_options(s))
        page = context.new_page()
        page.goto(s.base_url)
        print("Window opened. Sign in via ID.GOV.UA -- I am waiting and clicking nothing.")
        try:
            page.wait_for_url(re.compile(r"/cabinet"), timeout=timeout_minutes * 60_000)
        except PwTimeout:
            print("Did not get a login in time. Try again.")
            browser.close()
            return False
        page.wait_for_timeout(3000)
        check = page.evaluate(_FETCH_JS, "/api/v2/equeue/services")
        context.storage_state(path=str(path))
        restrict(path)
        browser.close()
    ok = check["status"] == 200
    print(f"Session saved: {path}" if ok
          else f"Session saved, but the API check returned HTTP {check['status']}.")
    return ok
