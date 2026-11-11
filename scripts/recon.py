"""Phase 0 -- recon of eq.hsc.gov.ua.

Opens a headed Chromium with NO action automation: the user signs in via
ID.GOV.UA and reaches the date/time picker themselves. The script only
PASSIVELY records /api/* network responses and DOM snapshots into the recon/
folder (it is in .gitignore because it contains personal data and cookies).

To finish: just close the browser window.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from playwright.sync_api import Page, Response, sync_playwright

OUT = Path("recon")
# RECON_URL -- which page to start from (the e-queue by default).
START_URL = os.environ.get("RECON_URL", "https://eq.hsc.gov.ua")
# Record only the official MVS service-center sites (*.hsc.gov.ua).
HOST_RE = re.compile(r"https://([a-z0-9-]+\.)*hsc\.gov\.ua/")
SKIP_RE = re.compile(r"/_next/static/|/akam/|\.(css|js|woff2?|png|svg|ico|jpg)(\?|$)")


def main() -> None:
    (OUT / "responses").mkdir(parents=True, exist_ok=True)
    (OUT / "dom").mkdir(parents=True, exist_ok=True)
    log = (OUT / "log.jsonl").open("a", encoding="utf-8")
    counter = {"resp": 0, "dom": 0, "dirty": True}

    def on_response(resp: Response) -> None:
        url = resp.url
        if not HOST_RE.match(url) or SKIP_RE.search(url):
            return
        req = resp.request
        ctype = resp.headers.get("content-type", "")
        counter["resp"] += 1
        n = counter["resp"]
        entry = {
            "n": n,
            "t": time.strftime("%H:%M:%S"),
            "method": req.method,
            "url": url,
            "status": resp.status,
            "ctype": ctype,
            "resource": req.resource_type,
            "post": req.post_data if req.method != "GET" else None,
            "req_headers": {
                k: v
                for k, v in req.headers.items()
                if k.lower() not in ("cookie", "authorization")
            },
        }
        try:
            # Cookie names and lifetimes only -- no values.
            cookies = []
            for h in resp.headers_array():
                if h["name"].lower() == "set-cookie":
                    parts = h["value"].split(";")
                    name = parts[0].split("=", 1)[0].strip()
                    life = [p.strip() for p in parts[1:] if p.strip().lower().startswith(("max-age", "expires"))]
                    cookies.append(f"{name} ({', '.join(life) or 'session'}, len {len(parts[0])})")
            if cookies:
                entry["set_cookie"] = cookies
                print(f"    set-cookie: {'; '.join(cookies)}", flush=True)
        except Exception:
            pass
        if "json" in ctype or req.resource_type in ("xhr", "fetch"):
            try:
                body = resp.text()
                (OUT / "responses" / f"{n:04d}.txt").write_text(body, encoding="utf-8")
                entry["body_file"] = f"responses/{n:04d}.txt"
                entry["body_len"] = len(body)
            except Exception as exc:  # the body may be unavailable (redirect, etc.)
                entry["body_err"] = str(exc)
        log.write(json.dumps(entry, ensure_ascii=False) + "\n")
        log.flush()
        counter["dirty"] = True
        print(f"[{n}] {req.method} {resp.status} {url[:140]}", flush=True)

    with sync_playwright() as pw:
        # RECON_CHANNEL=chrome -- use the installed Google Chrome
        # (needed if the downloaded Playwright Chromium does not start).
        channel = os.environ.get("RECON_CHANNEL") or None
        browser = pw.chromium.launch(headless=False, channel=channel)
        context = browser.new_context(locale="uk-UA", timezone_id="Europe/Kyiv")
        context.on("response", on_response)
        page = context.new_page()
        try:
            page.goto(START_URL)
        except Exception as exc:  # site did not respond -- keep the window, user can type the URL
            print(f"Could not open {START_URL}: {str(exc).splitlines()[0]}", flush=True)
        print("Browser opened. Sign in manually and reach the date/time picker.", flush=True)

        last_url = ""
        while True:
            pages: list[Page] = [p for p in context.pages if not p.is_closed()]
            if not pages or not browser.is_connected():
                break
            try:
                for p in pages:
                    p.wait_for_timeout(1500)
                    url = p.url
                    if HOST_RE.match(url) and (url != last_url or counter["dirty"]):
                        counter["dom"] += 1
                        counter["dirty"] = False
                        last_url = url
                        d = counter["dom"]
                        (OUT / "dom" / f"{d:04d}.html").write_text(
                            f"<!-- {url} -->\n" + p.content(), encoding="utf-8"
                        )
                # NOTE: do not call context.storage_state() here -- to collect
                # localStorage Playwright opens a temporary page for each origin,
                # which in headed mode looks like a window that keeps popping up
                # and stealing focus.
            except Exception:
                # the window was closed mid-operation
                if not [p for p in context.pages if not p.is_closed()]:
                    break
        print("Done: window closed, data in recon/", flush=True)


if __name__ == "__main__":
    main()
