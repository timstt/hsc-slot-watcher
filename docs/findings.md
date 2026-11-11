# Phase 0 -- reconnaissance results

Recon date: 2026-11-11. Method: headed Google Chrome under Playwright, login and
all clicks done manually by the user; the `scripts/recon.py` script only passively
recorded site responses. There was no state-changing request (every `/api/*`
request was a `GET`). Literal strings in the JSON examples below are the site's
own (Ukrainian) response data, kept verbatim.

## 1. Key points

| Question | Answer |
|---|---|
| Is there a JSON API? | **Yes**, `GET /api/v2/equeue/*`. No DOM parsing needed. |
| One request for all dates? | **No.** One request gives the list of dates, then a separate request per date. |
| Does it work without login? | No, `401 "Unauthorized"`. |
| Does it allow a headless browser? | **No -- `403 Access Denied` from Akamai** (see §6). This is a blocker. |

## 2. Step flow (as a human goes through it)

1. `https://eq.hsc.gov.ua/` -> `301` -> `https://eqn.hsc.gov.ua/` (a Next.js app).
2. The consent checkbox + the "Sign in via ID.GOV.UA" button
   -> `GET /api/v2/oauth/govid` -> `307` to id.gov.ua.
3. After login: `/openid/auth/govid?...` -> `/api/v2/oauth?...` -> `/cabinet`.
4. `/cabinet` loads `GET /api/v2/tickets` (the user's tickets).
5. "Book" -> `/cabinet/queue` -> `GET /api/v2/equeue/services`.
6. Pick the "Theory exam" service -> `GET /api/v2/equeue/departments?serviceId=8`.
7. Pick a center -> `GET /api/v2/equeue/days?serviceId=8&departmentId=142`.
8. Click a date -> `GET /api/v2/equeue/slots?serviceId=8&departmentId=142&date=2026-11-12T00:00:00`.
9. Then booking (`/api/v2/booking`, only visible in the JS code). **The monitor
   never calls this endpoint.**

## 3. Endpoints (all `GET`, base `https://eqn.hsc.gov.ua`)

Browser request headers: `accept: application/json, text/plain, */*`,
`referer: https://eqn.hsc.gov.ua/`, session cookies. No special tokens
(CSRF, Authorization) in the headers.

### `/api/v2/equeue/services`
```json
{"data":[ ..., {"id":8,"groupId":0,"title":"Теоретичний іспит "}, ...]}
```
`serviceId = 8`. The title has a trailing space -- compare after `strip()`.

### `/api/v2/equeue/departments?serviceId=8`
```json
{"data":[{"id":142,"name":"ТСЦ МВС № 7441 (надання послуг в укритті на час повітряної тривоги)",
  "latitude":51.514126,"longitude":31.347924,"region":"Чернігівська область",
  "district":"Чернігівський район","city":"м. Чернігів","street":"Шевченка",
  "building":"162а","office":null,"allowOnlineCount":1}, ...]}
```

Chernihiv-region centers for the theory exam:

| id | Exact name | City | Address |
|---|---|---|---|
| **142** | `ТСЦ МВС № 7441 (надання послуг в укритті на час повітряної тривоги)` | Chernihiv | Shevchenka 162a |
| 143 | `ТСЦ МВС № 7442` | Pryluky | Industrialna St. 6 |

### `/api/v2/equeue/days?serviceId=8&departmentId=142`
```json
{"data":[{"date":"2026-11-12T00:00:00"},{"date":"2026-11-13T00:00:00"}, ...]}
```
On 2026-11-11 it returned 15 dates: from 11-12 to 12-02 (21 calendar days ahead),
without Sundays and Mondays.

### `/api/v2/equeue/slots?serviceId=8&departmentId=142&date=2026-11-12T00:00:00`
```json
{"data":[{"startTime":"11:00:00","stopTime":"11:15:00"},
         {"startTime":"11:15:00","stopTime":"11:30:00"}, ...]}
```

### `/api/v2/tickets` (not needed by the monitor)
Fields: `ticketId, holdToTime, email, service, status, department, receiptNumber`.
Contains personal data -- do not log or store it.

## 4. Formats

- Date: `YYYY-MM-DDT00:00:00` -- no timezone, local Kyiv time. It is passed to the
  `date` parameter exactly as it came from `days`.
- Slot time: `HH:MM:SS`, 15-minute step, fields `startTime` / `stopTime`.
- There is no slot id -- the slot key is `(departmentId, date, startTime)`.
- A successful response is always wrapped as `{"data": [...]}`.

## 5. States

| State | What it looks like | Verification |
|---|---|---|
| No session / expired | `401`, `content-type: application/json`, body `"Unauthorized"` | verified without login |
| Anti-bot block | `403`, `text/html`, `<TITLE>Access Denied</TITLE>` (Akamai) | verified |
| Empty day | **unknown** -- recon opened only one date (11-12, 20 slots) | not verified |

Hypothesis for an empty day: `{"data":[]}`, or the date simply drops out of
`days`. The parser treats only `200` + JSON + a list under `data` as valid;
anything else is an error and the snapshot is not updated.

## 6. Blocker: Akamai anti-bot blocks headless

The site sits behind Akamai Bot Manager (`/akam/13/...` scripts, sensor `POST`s).

| Launch | Result |
|---|---|
| Headed Chrome under Playwright (as in recon) | works, `200` |
| Headless Chrome under Playwright (`HeadlessChrome/157` in the User-Agent) | `403 Access Denied` on the home page and on `/api/*` |

So "the monitor runs headless" cannot be done honestly. User-Agent spoofing,
stealth plugins, etc. would be an anti-bot bypass, which the project deliberately
avoids. The monitor therefore runs a headed (minimized) Chrome.

## 7. Additions after the first `once` run (11-11, 12:03-12:09)

### `days` needs a preceding "pick a center" step
In a fresh session `GET /api/v2/equeue/days...` returns **`204` with no body**
until `GET /api/v2/equeue/departments?serviceId=8` has run in that session. After
it, `days` immediately returns `200` with the list. This is a normal UI step, so
the cycle starts with it. `204` means a state error, not "no dates".

### Request rate limit (`429`) -- a blocker for a full snapshot
Response body is 19 bytes, `content-type: application/json`.

| Run | Pace | Result |
|---|---|---|
| 12:06:36-12:07:01 | 1-3 s pause | 10 requests `200`, the 11th -- `429` |
| 12:07:31-12:08:48 | 7-9 s pause | 9 requests `200`, the 10th -- `429` |

The second run started only 30 s after the first `429`, so either the limit
window is longer than a minute or earlier requests still counted. Exact limit
parameters are **unknown**. A full snapshot is 17 requests (center + dates +
15 dates).

### A working pace (measurement 12:47-12:54)
A **20-30 s** pause between requests: a full pass of 17 requests (center + dates +
15 dates) went through **with no `429`**, 346 slots. This is the default. The
limit seems to be time-window based (~10 requests/min), not a counter.

### Session lifetime -- 15 minutes, no renewal (measurement 12:47-13:17)
- The login cookie `__Secure-auth.access-token` is issued for exactly **15 min**.
- The site does **not** renew it: neither API requests, nor opening `/cabinet`,
  nor tab activity move the deadline. At minute 15 the page updates another cookie
  once and logs out.
- The site code confirms it: on `401` it runs `oauth/signOut` and redirects to `/`;
  there is no refresh mechanism.
- Consequence: long-term monitoring without re-login is impossible. A **windowed
  mode** is implemented -- the user logs in, the monitor watches for 15 min (see README).

### Other
- `GET /api/v2/oauth/session` with a live session -> `200`, JSON with an `account` key.
- Headed Chrome in a minimized window is allowed by Akamai.
- `days` returns `204` until `departments` has been requested in the session.

## 8. Still unknown
- Exact rate-limit parameters (how many requests over what window).
- Whether a `204` on `slots` means an empty day (currently treated as an error).
- What an empty day looks like (§5).

## 9. Fixtures

Anonymized real responses live in `tests/fixtures/`:
`services.json`, `departments_chernihiv_region.json`, `days.json`,
`slots_2026-11-12.json`, `unauthorized_401.json`, `akamai_403.html`.
The raw recon dump (`recon/`) contains personal data and is not committed.
