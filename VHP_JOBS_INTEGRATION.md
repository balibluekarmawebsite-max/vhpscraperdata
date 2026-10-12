# VHP on-demand scraping — dashboard integration spec

This describes the **job queue** the dashboard needs so a person can click
“Request scrape” on the dashboard and have the office PC pull that exact report
and period, then upload it back — no manual downloads, no waiting for the daily
schedule.

It is the companion to the existing upload contract (`/api/import`). **That
endpoint does not change.** This spec only adds a small queue and two endpoints
on top of it.

> **Who builds what**
> - **Dashboard (you / your dashboard agent):** the `scrape_jobs` table, the two
>   endpoints below, and a “Request scrape” button. This document is the
>   contract to build against.
> - **Scraper (the office PC, already built):** `poller.py` claims jobs, scrapes,
>   uploads via `/api/import`, and reports each job’s result. You don’t build this
>   side — it’s done and matches this spec.

---

## 1. Why it’s a “pull”, not a “push”

The office PC sits behind the office router — the dashboard **cannot open a
connection to it**. But the PC can open connections **out** to the dashboard any
time (that’s exactly how `/api/import` already works).

So the dashboard **writes down a job**, and the PC **asks for jobs** on a short
loop and runs them. Nothing needs to be opened on the office network.

```
   DASHBOARD (VPS)                                OFFICE PC (poller.py)
   ┌──────────────────────────┐
   │ [Request scrape] button  │  user picks property + report + period
   │   INSERT scrape_jobs ─────────► row status = 'queued'
   └──────────────────────────┘             ▲
                                             │ POST /api/jobs/claim   ◄── every ~2 min
                                             │   (hands back oldest queued job,
                                             │    atomically flips it to 'running')
                                             │
                                             │   scraper attaches to Chrome,
                                             │   sets the requested period,
                                             │   downloads the CSV
                                             │
           POST /api/import   ◄──────────────┤   uploads the CSV (unchanged
             (property, section,             │    endpoint — your existing
              week_ending, source, file)     │    parser writes the data)
                                             │
           POST /api/jobs/{id}/result ◄──────┘   status = 'done'  (or 'failed'
             (status, message, written)           + the reason)
   ┌──────────────────────────┐
   │ job row shows: Done ✓     │
   │   or: Failed — <reason>   │
   └──────────────────────────┘
```

The user doesn’t care how fast it starts, only that it pulls the **correct**
data — so the scraper defaults to checking every ~2 minutes and always sets (and
verifies) the exact requested window before exporting.

---

## 2. The `scrape_jobs` table

Suggested columns (adapt names to your schema; the scraper only reads/writes
through the two endpoints, so your internal names are up to you):

| column         | type        | notes                                                        |
|----------------|-------------|--------------------------------------------------------------|
| `id`           | text / int  | primary key; returned to the scraper and used in the result URL |
| `property`     | text        | `BKDS` \| `BKDU` \| `BKV`                                     |
| `section`      | text        | `B` (yearly-forecast) \| `C` (reservation-by-creation-date)  |
| `start`        | date        | section C only — first day of the range (inclusive)          |
| `end`          | date        | section C only — last day of the range (inclusive)           |
| `month`        | text        | section B only — `YYYY-MM`                                    |
| `week_ending`  | date        | the week/key this fills (see §6); recommended for C          |
| `status`       | text        | `queued` → `running` → `done` \| `failed`                    |
| `worker`       | text        | which PC claimed it (set on claim)                           |
| `message`      | text        | human-readable result/reason (set on result)                 |
| `written`      | int         | rows written (echoed from `/api/import`, on result)          |
| `requested_at` | timestamptz | when the user clicked                                        |
| `claimed_at`   | timestamptz | set on claim (used for the stale-lease, §7)                  |
| `finished_at`  | timestamptz | set on result                                                |

---

## 3. Endpoint 1 — claim the next job

Hand the oldest `queued` job to the caller and **atomically** flip it to
`running`. This is the only tricky part — it must be race-safe so a job is never
handed to two workers (even though today there’s only one PC).

```
POST /api/jobs/claim
Authorization: Bearer <token>
Content-Type: application/json

{ "worker": "office-pc-1" }
```

**Response — a job is waiting (HTTP 200):**
```json
{
  "job": {
    "id": "job_123",
    "property": "BKDS",
    "section": "C",
    "start": "2026-09-25",
    "end": "2026-10-01",
    "week_ending": "2026-10-01"
  }
}
```

**Response — queue empty (HTTP 200, or 204 No Content):**
```json
{ "job": null }
```

Implementation note (Postgres): do the select-and-mark in one statement so two
claims can never grab the same row:

```sql
UPDATE scrape_jobs
SET status = 'running', worker = $1, claimed_at = now()
WHERE id = (
  SELECT id FROM scrape_jobs
  WHERE status = 'queued'
  ORDER BY requested_at
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
RETURNING id, property, section, start, "end", month, week_ending;
```

---

## 4. Endpoint 2 — report a job’s result

```
POST /api/jobs/{id}/result
Authorization: Bearer <token>
Content-Type: application/json

{
  "worker": "office-pc-1",
  "status": "done",                 // "done" | "failed"
  "message": "Section C updated",   // human-readable; shown in the UI
  "week_ending": "2026-10-08",      // the week the scraper filled (set for C; may be null for B)
  "written": 7,                     // rows written (from /api/import), optional
  "import": { "ok": true, "property": "BKDS", "section": "C", "written": 7 }
}
```

> `week_ending` here is the week the scraper **actually filled** (the Thursday it
> computed for section C), sent explicitly — the scraper does not rely on
> `/api/import` echoing it back.

**Response (HTTP 200):** `{ "ok": true }`

- Set `status`, `message`, `written`, `finished_at` on the row.
- **Be idempotent:** a repeat `result` for an already-closed job should still
  return `{ "ok": true }` (the scraper may retry if the first response was lost).
- A `status: "failed"` with a `message` is a normal outcome, not an error —
  show the reason to the user (e.g. *“that week is locked/approved — reopen it to
  allow this update”*, or *“the BKDU window isn’t logged into VHP”*). The scraper
  phrases these in plain language.

---

## 5. The data path is unchanged

The scraper **still uploads the CSV through your existing `/api/import`** (same
fields: `property`, `section`, `week_ending`, `source`, `file`). Your current
parser writes the data exactly as it does for the scheduled runs. The only
difference on-demand is that `week_ending` carries the **requested** week, so a
back-filled past week lands in the right place rather than today’s.

The `/api/jobs/{id}/result` call happens **after** the import succeeds and is
only status bookkeeping for the UI. You do **not** need to change `/api/import`.

---

## 6. What the scraper reads from each job

**Section C — reservation-by-creation-date (ONE Fri→Thu week):**

Section C is a **full replace of one week’s** market-segment rows, so each job is
**exactly one Fri→Thu week** — never a multi-week range (a month of rows could
only carry one week label, which would silently overwrite the wrong week).

- `start` — **must be a Friday.**
- `end` — **must be the following Thursday** (i.e. `end = start + 6 days`).
- `week_ending` — **optional and, if sent, must equal `end`** (the Thursday). The
  scraper uploads under `end` either way. (This lines up with your
  `/weekly/<prop>/<week_ending>/` URLs, where `<week_ending>` is that Thursday.)

The scraper sets VHP’s date filter to exactly `start..end`, verifies it on
screen, and only then exports; if it can’t set+verify the week it **fails the job**
rather than upload a wrong window. A job whose range isn’t a single Fri→Thu week
(wrong day-of-week, or spanning more than 7 days) is **rejected up front** with a
clear message. **To back-fill a month, queue one job per week.**

**Section B — yearly-forecast (a whole calendar YEAR):**

The yearly-forecast report shows 12 months **from a start month** (an Element-UI
month picker). The scraper always sets that start to **January of the target
year**, so the export is a clean **Jan → Dec of that year**:
- closed months = **actuals**, the current month updates, later months = forecast.

Because the columns are always `Jan … Dec` in order, your **existing month-name
matching is already correct — no dashboard change is needed.** (The reason the
old data was wrong is the daily run used to start at the *current* month, so its
`Jan…Sep` columns were actually *next* year’s forecast. Starting at January fixes
that.)

- `month` — `YYYY-MM` (or `year` — `YYYY`). The scraper takes the **year** from it
  and starts the report at that January. So a user picking *any* date in 2026
  backfills the **whole 2026 calendar year** in one request. No `month` / `year`
  → the current year.
- A bad `month` (not `YYYY-MM`) or `year` is rejected up front.
- `week_ending` — not used for B; your parser keys section B by month name.

> So **one Section B request = one full year.** To backfill 2026, request 2026
> (e.g. the UI’s month picker set to Jan 2026). The daily scheduled run already
> keeps the **current** year refreshed the same way.

Property is `BKDS` / `BKDU` / `BKV` (case-insensitive on the scraper side).

---

## 7. Concurrency, retries, and the stale lease

- **One job at a time** on the PC. The scraper also coordinates on-demand jobs
  with the scheduled 6am/Friday runs and the hourly keep-alive locally (a file
  lock), so Chrome is only ever driven by one of them.
- **Retries:**
  - `result` — the scraper retries on network errors and `429 / 500 / 502 / 503 /
    504` with backoff, so make it **idempotent** (§4): a repeat for an
    already-closed job just returns `{ "ok": true }`.
  - `claim` — the scraper does **NOT** retry (a single attempt). `claim` consumes
    a different job each time, so a blind retry after a lost response would strand
    the first job; instead a missed claim is simply retried on the next poll. You
    don’t need to do anything special — just don’t rely on the scraper retrying a
    failed claim.
- **Stale lease (important):** if a job is `running` but gets **no result within
  ~60 minutes**, treat the worker as crashed and return the job to `queued` (or
  allow it to be re-claimed). That way a power cut or a closed Chrome window can
  never strand a job in `running` forever.
  - **Why 60, not 30:** an on-demand job can legitimately **wait** for Chrome
    behind the scheduled daily/weekly run before it even starts scraping. The
    scraper waits up to ~25 min for the lock, then scrapes+uploads (a few min), so
    a perfectly healthy job can take ~30 min end-to-end. A 60-min lease leaves
    margin so the dashboard doesn’t re-queue (and duplicate) a job that’s still
    running normally. The scraper reports `failed`/`done` itself whenever it can;
    the lease only covers a true crash before it can report.

---

## 8. Section B — RESOLVED (how past-year backfill works)

This was an open question; it’s now solved. The yearly-forecast report **does
not** contain closed-month actuals by default — its standard export starts at the
*current* month and runs 12 months **forward**, so its `Jan…Sep` columns were
*next* year’s forecast (that’s why `Jan` showed ~33% instead of the real ~77%).

The fix: the report has a **month picker** (`data-cy="field-month"`). The scraper
now sets it to **January of the target year**, and the export comes back as a
clean **Jan → Dec of that year** — closed months as actuals. Verified against
live data (e.g. BKDS `Jan 2026 = 77.42%` actual, vs the forward export’s
`33.33%`).

**Dashboard impact: none.** Because every Section B export is `Jan … Dec` in
order, your existing month-name matching lands each column on the right row. You
do **not** need to change `/api/import` or your parser. Just:
- **Backfill a past year:** the user requests Section B for that year (any date in
  it) → one upload fills all 12 months correctly.
- **Current year:** the daily scheduled run now also starts at January, so it
  keeps the current year’s table correct and fresh automatically (this replaces
  the old behaviour that was filling `Jan…Sep` with next-year forecast).

---

## 9. Auth

- Use the **same Bearer token** as `/api/import` (`IMPORT_API_TOKEN`) for both
  `/api/jobs/claim` and `/api/jobs/{id}/result` — simplest, and the scraper
  defaults to it.
- If you’d rather the queue use its own secret, set it on the PC as
  `VHP_JOBS_TOKEN` and the scraper will use that instead. Either way, **both
  endpoints must require the token** (claim hands out work; result writes state).

---

## 10. Quick self-test (once the two endpoints exist)

From the office PC (token already in the environment):

```bat
REM 1) Queue a job on the dashboard (your "Request scrape" button, or by hand).
REM 2) Then run ONE cycle and watch it claim → scrape → upload → report:
C:\Python38\python.exe poller.py --once
```

`--once` claims and runs at most one job then exits (no looping) — ideal for the
first end-to-end test. `--drain` runs everything queued and exits. With no flag,
`poller.py` runs forever (that’s what `run-poll.bat` uses in production).

---

## 11. Checklist for the dashboard side

- [ ] `scrape_jobs` table (§2)
- [ ] `POST /api/jobs/claim` — atomic claim of the oldest queued job (§3)
- [ ] `POST /api/jobs/{id}/result` — idempotent status update (§4)
- [ ] Stale-lease: `running` with no result in ~60 min → re-queue (§7)
- [ ] Both endpoints require the Bearer token (§9)
- [ ] “Request scrape” UI: pick property + section + period → INSERT a `queued` row
- [ ] Show each job’s status + message (queued / running / done / failed)
- [ ] Section B: nothing to change — month-name matching already works (§8)

Everything the scraper sends and expects is fixed by this document — build to it
and the two sides line up the first time.
