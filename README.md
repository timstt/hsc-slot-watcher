# hsc-slot-watcher

A monitor for the Ukrainian MVS service-center electronic queue (eq.hsc.gov.ua)
that watches for free **theory driving-exam** slots in **Chernihiv** and sends a
Telegram message the moment a new slot **no later than 10:30** appears.

**It only notifies. You book yourself, manually.** There is no state-changing
call anywhere in the project: it uses read-only GET requests (the list of dates
and the slots of a date). The booking endpoint is never called.

> User-facing Telegram messages are in Ukrainian on purpose (that is the
> product's language). The code, comments and this README are in English.

## What it deliberately does not do

- it does not log in for you via ID.GOV.UA / Diia.Signature / BankID / qualified
  signature, and it does not store your credentials;
- it does not book or cancel anything;
- it does not bypass captcha, blocking, anti-bot or geo-blocking. A block leads
  to a pause and a message to you, not a workaround.

## It runs on your computer, in a visible browser

The site sits behind Akamai anti-bot, which **blocks a headless browser**
(`403 Access Denied`). So the monitor launches a regular Google Chrome in a
minimized window and works while the computer is on. There is no VPS/Docker
deployment: a server has no screen, and faking a real browser would be a bypass.
Details are in [docs/findings.md](docs/findings.md).

## The key constraint: 15-minute windows

The site keeps your login for **exactly 15 minutes** and does not renew it
(verified -- see [docs/findings.md](docs/findings.md) §7). There is no legitimate
way to hold the session longer without logging in again, and the program
deliberately does not automate the login or bypass the protection.

So the monitor works in **windows**:

1. You run `python -m watcher login` and sign in (fastest via Diia.Signature).
2. For the next ~15 minutes the monitor scans every date on its own and sends
   new slots `<= 10:30` to Telegram. One full pass of 15 dates takes about
   7 minutes, so a window fits 1-2 passes.
3. When the login expires, the bot sends **one** "re-login needed" message and
   pauses. Run `login` again whenever you want the next window -- the monitor
   picks up the new session automatically.

The snapshot persists between windows, so the first pass of each new window
reports what was freed since you were last logged in. Honestly: **a cancellation
that happens between your windows will not be seen** -- only what exists while
you are logged in. That is a limitation of the site, not of the program.

## 1. Install (Windows)

Requires Python 3.12 and Google Chrome.

```powershell
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
```

Without `uv`: `py -3.12 -m venv .venv`, then `.venv\Scripts\pip install -e ".[dev]"`.

All commands below are run through `.venv\Scripts\python.exe -m watcher ...`
(shortened to `python -m watcher ...`).

## 2. Telegram bot

1. In Telegram open **@BotFather** -> `/newbot` -> choose a name and a username
   (it must end with `bot`). BotFather gives you a **token**.
2. Copy `.env.example` to `.env` and put the token into `TELEGRAM_BOT_TOKEN=`.
3. Open the chat with your bot and press **Start** (without it the bot cannot
   write to you).
4. Find your chat_id:
   ```powershell
   python -m watcher chat-id
   ```
   Copy the printed `TELEGRAM_CHAT_ID=...` line into `.env`.
5. Verify:
   ```powershell
   python -m watcher test-notify
   ```
   A test message should arrive in Telegram.

The bot writes only to this chat_id and silently ignores messages from anyone else.

## 3. Login

```powershell
python -m watcher login
```

A Chrome window opens. Tick the consent box, click "Sign in via ID.GOV.UA" and
sign in as usual. As soon as the cabinet opens, the program saves the session to
`data/storage_state.json` and closes the window itself. It clicks nothing for you
-- it only waits.

## 4. Checks

```powershell
python -m watcher simulate            # test scenarios -> [ТЕСТ] messages in Telegram
python -m watcher simulate --dry-run  # the same, but printed to the console
python -m watcher once                # one real cycle, console output, no notifications
```

`once` shows every date with its slot count and separately the times `<= 10:30`.

## 5. Running the monitor

```powershell
python -m watcher run
```

Stop with `Ctrl+C`. Logs: `logs/watcher.log` (rotating, 5 files of 1 MB, no secrets).

What it does:

- each cycle it snapshots the slots of every date (requests are strictly serial,
  with a 20-30 s pause between them);
- it compares against the previous snapshot and reports only **new** slots `<= 10:30`;
- a slot that disappeared and came back counts as new -- it reports it again;
- the first run: one message with all current slots `<= 10:30`;
- it ignores past slots and ones starting less than 2 hours away;
- if the site returns an error or a broken response, the snapshot is left untouched.

Schedule (pause between cycles): normally ~60 s (+/-30%), 00:00-05:30 -- ~20 min.
The pause between individual requests is 20-30 s (the measured safe pace).

> **Site rate limit.** The site answers `429` if there are too many requests
> (roughly from the 10th-11th in a row; exact rules unknown, see
> [docs/findings.md](docs/findings.md) §7). The monitor does not bypass it: on a
> `429` it pauses for 60 min and messages you. While the monitor runs, do not
> browse the e-queue in a browser without need -- the requests are likely counted
> together.

### Bot commands

| Command | What it does |
|---|---|
| `/status` | time of the last successful cycle, session state, whether notifications are on |
| `/slots` | current free slots `<= 10:30` (from the last cycle's snapshot) |
| `/pause` | turn off slot notifications (e.g. once you have booked) |
| `/resume` | turn them back on |

The commands work while `run` is active and change nothing on the MVS site.

### Service messages

| Message | What to do |
|---|---|
| re-login needed | `python -m watcher login`; the monitor picks up the new session within ~2 min |
| access blocked | check the site in a normal browser; the monitor pauses for 60 min |
| no successful cycles > 30 min | check your internet and `logs/watcher.log` |
| heartbeat | the daily "alive" message at 09:00, nothing to do |

Each of these repeats no more than once every 4 hours. During quiet hours
(00:00-06:00) everything arrives silently.

## 6. Autostart and 24/7

### Keep the computer from sleeping

Settings -> System -> Power -> "Sleep" -> **Never** (on AC). The screen may turn
off, but the machine must not sleep.

### Autostart after Windows login

The browser window needs a desktop, so the task must run "only when the user is
logged on". Start the monitor **through its own PowerShell window** (not python.exe
directly): otherwise Playwright can fail with `[WinError 6]` because it does not
get a valid console. In PowerShell (replace the path with yours):

```powershell
$dir = "D:\cod\hsc-slot-watcher"
$cmd = "Set-Location '$dir'; .\.venv\Scripts\python.exe -m watcher run"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-WindowStyle Minimized -NoProfile -Command `"$cmd`"" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit 0
Register-ScheduledTask -TaskName "hsc-slot-watcher" -Action $action -Trigger $trigger -Settings $settings
```

Remove it: `Unregister-ScheduledTask -TaskName "hsc-slot-watcher"`.

> If you start the monitor by hand, just run `python -m watcher run` in a normal
> terminal window. Do not start it with redirected output detached (e.g.
> `Start-Process -RedirectStandardOutput`): that is what triggers WinError 6.

### Health check

```powershell
python -m watcher health --max-age 30
```

Exit code 0 if the last successful cycle is within 30 minutes.

## 7. Configuration

Everything is set in `.env`; the full list with explanations is in
[.env.example](.env.example). The most important:

| Variable | Default | Meaning |
|---|---|---|
| `LATEST_TIME` | `10:30` | the latest interesting slot time |
| `LATEST_INCLUSIVE` | `true` | `false` -- strictly earlier |
| `DATE_FROM` / `DATE_TO` | - | limit the date range |
| `SERVICE_CENTERS` | Chernihiv (142) | list of centers, JSON |
| `SCAN_MODE` | `full` | `tiered` -- near dates more often, far dates in rotation |
| `QUIET_START` / `QUIET_END` | `00:00`-`06:00` | quiet hours |
| `INTERVAL_SECONDS` | `60` | pause between cycles |

Add Pryluky: `SERVICE_CENTERS=[{"id":142,"name":"Чернігів"},{"id":143,"name":"Прилуки"}]`.
The center name appears in every message.

Fallback channel -- email: set `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`
(for Gmail an App Password), `EMAIL_TO`. It is used only when Telegram is
unavailable after several attempts.

## 8. Statistics

```powershell
python -m watcher stats
```

Shows the hours of the day and days of the week when new slots `<= 10:30` appear
most often.

## 9. Other commands

- `python -m watcher check-access` -- whether the site opens from this computer
  (without logging in). If it is blocked, it says so honestly and bypasses nothing.

## 10. Security

- Secrets live only in `.env` and `data/storage_state.json`; both are in `.gitignore`.
- `storage_state.json` is your active session on the MVS site. Do not share it.
- On Linux/macOS files are created with mode 600. On Windows chmod is a no-op --
  keep the project under your user profile, not in a shared folder.
- The bot token never reaches the logs (a logging filter redacts it).
- If the token leaked anywhere: @BotFather -> `/revoke` -> put the new one in `.env`.

## 11. Backup and updates

Saving `.env` is enough. Losing `data/watcher.sqlite` is not critical: the first
cycle simply sends the current morning slots. Update: `git pull`, then restart
`run` -- the state is kept in `data/`.

## 12. Tests

```powershell
python -m pytest
```

Tests run on real (anonymized) site responses from `tests/fixtures/`, with the
network mocked.

## Structure

```
watcher/
  browser/    Chrome via Playwright, manual login, read-only GET requests
  parser/     API response parsing, detection of 401/403/429/broken responses
  core/       snapshot diff, filter, one cycle
  scheduler/  schedule, quiet hours, the monitor loop and error handling
  notifier/   Telegram, email, bot commands, message formatting
  storage/    SQLite: snapshot, state, statistics
  simulate.py test scenarios
  cli.py      commands
docs/findings.md   results of the site reconnaissance
```

## Tech stack

Python 3.12, Playwright (Chromium), SQLite, pydantic-settings, httpx, pytest.
