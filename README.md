# VHP Report Scraper

Automatically logs into VHP, downloads the reports we need for each property
(**BKDS**, **BKDU**, **BKV**), and pushes each file to the analytics dashboard
at `analytics.bluekarmasecrets.com` → Postgres, with **no manual uploads**.
Runs unattended daily and weekly on the office PC via Windows Task Scheduler.

---

## ⚠️ Hard constraints — read this first

The runner PC is **Windows 7 SP1** (Intel i3-3220, 8 GB RAM, 64-bit). The modern
browser-automation stack dropped Windows 7, so **every version below is pinned
and must not be "updated to latest."**

| Component | Pinned version | Why |
|---|---|---|
| OS | Windows 7 SP1 64-bit | the machine we have |
| Python | **3.8.10** (last 3.8 Windows installer) | Python 3.9+ refuses to run on Win7 |
| Selenium | **4.9.1** | newer releases may break on Py3.8 / Win7 |
| Browser | **Google Chrome 109** (last for Win7) | Chrome 110+ won't install on Win7 |
| Driver | **ChromeDriver 109** (matching build) | must match Chrome 109 exactly |
| Fallback | Firefox ESR 115 + geckodriver | last Firefox that runs on Win7 |

**Rules**
- Do **not** let Selenium Manager auto-fetch a driver — it pulls a too-new one.
  We point Selenium at the local `chromedriver.exe` (v109) explicitly in
  `browser.py` / `config.py`.
- Turn **off** Chrome auto-update so it can't jump past 109 (see below).
- `requests==2.31.0` for the upload (works on Py3.8).

> **Security note:** this is an end-of-life OS handling PMS data. Acceptable as
> a quick win on the internal network — but keep migrating to a newer PC or the
> VPS on the roadmap.

---

## Project layout

```
vhp-scraper/
  main.py            # orchestrates: loop properties x due reports
  config.py          # properties, reports, upload settings — EDIT HERE
  browser.py         # builds the Selenium Chrome driver (explicit v109 driver)
  vhp.py             # navigate + export + wait-for-download, per report
  uploader.py        # POST file to dashboard
  login.py           # one-time manual login per property (saves session)
  run.bat            # wrapper Task Scheduler calls
  requirements.txt
  chromedriver.exe   # v109 — download per machine, NOT in git
  profiles/          # one Chrome user-data-dir per property (holds login)
  downloads/         # downloaded report files
  logs/              # per-run logs
```

`profiles/`, `downloads/`, `logs/` and `chromedriver.exe` are git-ignored:
they hold logins, PMS data, and a per-machine binary.

---

## One-time setup on the Windows 7 PC

1. **Install Python 3.8.10** (64-bit) to `C:\Python38`. Tick "Add to PATH" or
   use the full path in `run.bat`.
2. **Install Google Chrome 109** (the last Win7 build). Then **disable
   auto-update** so it can't jump past 109:
   - Services → stop & disable `Google Update Service (gupdate)` and
     `(gupdatem)`, **and**
   - delete/rename the scheduled `GoogleUpdate` tasks in Task Scheduler.
3. **Download ChromeDriver 109** (build matching your exact Chrome 109.x) and
   put `chromedriver.exe` in the project root (next to `main.py`). Verify:
   `chromedriver.exe --version` → should print `ChromeDriver 109.x`.
4. **Clone/copy this project** to `C:\vhp-scraper` (or edit the path in
   `run.bat`).
5. **Install deps**:
   ```
   C:\Python38\python.exe -m pip install -r requirements.txt
   ```
6. **Smoke-test**: `C:\Python38\python.exe -c "import selenium, requests; print(selenium.__version__)"`
   → should print `4.9.1`.

---

## Configure (`config.py`)

Edit **only** `config.py` for normal setup:

- **PROPERTIES** — paste each property's real VHP `home_url`.
- **REPORTS** — replace the two examples with the exact reports you need; set
  each one's `cadence` (`daily` / `weekly` / `both`), its `url` (deep-link) or
  leave `None` to use recorded navigation, its `export_label`, and `date_range`.
- **UPLOAD** — confirm the dashboard endpoint `url` and form `field` name from
  the dashboard repo. If it needs auth, set the token via the
  `VHP_UPLOAD_TOKEN` environment variable (preferred over pasting it in code).

---

## Record the per-report navigation (`vhp.py`)

VHP's buttons differ per install, so `vhp.py` ships with **TODO templates**:

- `open_report()` — if a report has no deep-link `url`, fill in the menu clicks.
- `set_date_range()` — wire the computed start/end dates into VHP's date fields.
- `click_export()` — already matches a button/link by its visible label.

Use the **Selenium IDE** browser extension (or UI.Vision) to record your clicks,
then translate them here. **Prefer stable locators** — visible link text /
labels — over auto-generated absolute XPaths, which are the usual reason an
automation silently breaks later.

---

## Log in once per property (`login.py`)

```
C:\Python38\python.exe login.py bkds
C:\Python38\python.exe login.py bkdu
C:\Python38\python.exe login.py bkv
```

Chrome opens with that property's isolated profile. Log in by hand, return to
the console, press Enter — the session is saved under `profiles/<code>` and
reused by every later run. `main.py` detects a login screen and skips that
property (logging `re-login needed: <code>`) so the others still run.

> **If VHP is actually one login with a property selector** (not three
> accounts), use a single shared profile and add a "select property" step in
> `vhp.open_report()` instead of three profiles.

---

## Run manually

```
C:\Python38\python.exe main.py daily     # daily + both reports
C:\Python38\python.exe main.py weekly    # weekly + both reports
C:\Python38\python.exe main.py both      # everything
```

Each run writes a timestamped log to `logs/` and echoes to the console. One
property or report failing is logged and the rest continue.

---

## Schedule (Windows Task Scheduler)

Create two tasks pointing at `run.bat`:

- **Daily** → argument `daily` (e.g. 06:00 every day)
- **Weekly** → argument `weekly` (e.g. Monday 06:30)

Because the browser is **visible (headed)**, choose **"Run only when user is
logged on"** and keep the PC logged in. Pick quiet times.

---

## Error handling & alerts

- Each property and each report is wrapped in `try/except` — one failure logs
  and the rest continue.
- Per-run log lines: timestamp, property, report, saved path, upload status.
- Optional Slack alert on failures: set `VHP_SLACK_WEBHOOK` to a webhook URL.

---

## Build order (milestones)

1. **Environment** — Python 3.8.10, Chrome 109 + ChromeDriver 109, deps
   installed; confirm Selenium opens Chrome 109.
2. **Login** — `login.py` working for BKDS; session persists.
3. **One report** — fill `vhp.py` for a single BKDS report; file lands in
   `downloads/`.
4. **Upload** — confirm rows appear under BKDS in the dashboard.
5. **Generalize** — full `REPORTS` list across all three properties.
6. **Schedule** — `run.bat` + Task Scheduler; one unattended daily run E2E.
7. **Harden** — logging + failure alerts.

---

## Open questions to confirm during the build

- Is VHP a real web page or a streamed/remote screen? (Inspect a data table:
  real HTML rows vs one big `<canvas>`. If canvas → Selenium can't read it;
  switch to UI.Vision + OCR.)
- Does Export download a file, or pop a Windows "Save As" dialog? (A dialog
  means a streamed app → needs AutoHotkey.)
- Does each report have its own URL (deep-link) or only menu navigation?
- Exactly which reports, and which are daily vs weekly?
- Three separate VHP logins, or one login with a property switch?
- Dashboard upload endpoint path + field name (+ any auth token)?
- Export format (xlsx / csv) and how the date range is set.
