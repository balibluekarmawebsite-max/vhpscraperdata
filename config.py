"""
config.py — the ONE place you edit for day-to-day setup.

Everything the scraper needs to know about *your* VHP + dashboard lives here:
which properties to log into, which reports to pull, and where to upload them.

Nothing in the other files should need editing for normal operation.
"""
import os
import socket

# Hostname for the JOBS "worker" id, computed defensively: a hostname lookup must
# never break `import config` (which the scheduled run also does).
try:
    _HOSTNAME = socket.gethostname()
except Exception:  # noqa: BLE001
    _HOSTNAME = ""

# Absolute path of this project folder. Everything else is derived from it so
# the project works no matter where it's checked out (C:\vhp-scraper, etc.).
BASE = os.path.dirname(os.path.abspath(__file__))

PROFILES_DIR = os.path.join(BASE, "profiles")
DOWNLOADS_DIR = os.path.join(BASE, "downloads")
LOGS_DIR = os.path.join(BASE, "logs")

# Path to ChromeDriver 109. On Windows this is chromedriver.exe sitting next to
# the code. We point Selenium at it explicitly so Selenium Manager never
# auto-downloads a newer (Win7-incompatible) driver. See README section "Driver".
CHROMEDRIVER_PATH = os.path.join(
    BASE, "chromedriver.exe" if os.name == "nt" else "chromedriver"
)


# ---------------------------------------------------------------------------
# DASHBOARD TOKEN (resolved once, used by UPLOAD + JOBS below)
# ---------------------------------------------------------------------------
def _read_secret_file(*names):
    """Read a one-line secret from the first existing git-ignored file in BASE.
    This is the RELIABLE source for scheduled runs: Windows Task Scheduler does
    not always inherit a setx'd user environment variable, so the daily/weekly
    tasks can end up with an empty token even when your interactive session has
    it. A file next to the code is read the same way by every run."""
    for name in names:
        p = os.path.join(BASE, name)
        try:
            if os.path.isfile(p):
                with open(p, "r") as fh:
                    val = fh.read().strip()
                if val:
                    return val
        except Exception:  # noqa: BLE001
            pass
    return ""


# The dashboard API token. Order of precedence:
#   1. IMPORT_API_TOKEN environment variable (works for interactive runs)
#   2. import_token.txt / .import_token in C:\vhp-scraper (works for EVERY run,
#      including scheduled tasks — put the token on one line, no quotes).
# The file is git-ignored, so the secret is never committed.
IMPORT_API_TOKEN = (os.environ.get("IMPORT_API_TOKEN", "")
                    or _read_secret_file("import_token.txt", ".import_token"))

# ---------------------------------------------------------------------------
# PROPERTIES
# ---------------------------------------------------------------------------
# One entry per property. Each `profile` folder isolates that property's login
# (like having 3 separate Chrome profiles), so BKDS / BKDU / BKV never clash.
#
# TODO: paste the real VHP home URL for each property into `home_url`.
#
# NOTE: If VHP is actually ONE login with a property picker (not three separate
# accounts), see README section "Login model" — you'd use a single profile and
# a "select property" step instead of three profiles.
PROPERTIES = [
    # All 3 properties use the SAME login page (https://e1-vhp.com/login) with
    # DIFFERENT accounts. Each property's own Chrome profile holds its saved
    # session, so the 3 logins never clash.
    # `debug_port` = the remote-control port each property's Chrome window is
    # opened on (by open-<code>.bat). The scraper attaches to that port and
    # drives the window you already logged into. One port per property.
    {
        "code": "bkds",
        "name": "Blue Karma Dijiwa Seminyak",
        "profile": os.path.join(PROFILES_DIR, "bkds"),
        "home_url": "https://e1-vhp.com/login",
        "debug_port": 9222,
    },
    {
        "code": "bkdu",
        "name": "Blue Karma Dijiwa Ubud",
        "profile": os.path.join(PROFILES_DIR, "bkdu"),
        "home_url": "https://e1-vhp.com/login",
        "debug_port": 9223,
    },
    {
        "code": "bkv",
        "name": "Blue Karma Village",
        "profile": os.path.join(PROFILES_DIR, "bkv"),
        "home_url": "https://e1-vhp.com/login",
        "debug_port": 9224,
    },
]

# ---------------------------------------------------------------------------
# REPORTS
# ---------------------------------------------------------------------------
# Defined ONCE and reused for every property (same VHP UI across properties).
# Every report is pulled the same way (handled in vhp.py):
#   open the URL -> set the period -> click SEARCH -> Print -> "Print CSV"
#   -> the CSV downloads -> we rename + upload it.
#
# Fields:
#   name     : report slug. Matches the VHP page URL AND the CSV filename VHP
#              produces, and is used in our saved filename + upload tag.
#   cadence  : "daily", "weekly", "monthly", or "both" — which run picks it up.
#   url      : deep-link to the report page.
#   period   : how to set the date filter before searching (handled in vhp.py):
#                "last_7_days"   -> the 7 days ending today (today-6 .. today)
#                "current_month" -> 1st of this month .. today
#                {"days_back": N, "include_today": bool}
#                                -> trailing N-day window; include_today False
#                                   ends on yesterday (today-N .. today-1)
#                None            -> leave VHP's default, just press SEARCH
#   period_required (optional): True = if the date range can't be set AND
#                verified, SKIP the report instead of uploading a wrong-period
#                CSV. Leave OFF until set_period is confirmed for that report.
REPORTS = [
    {
        "name": "yearly-forecast-of-room-occupancy",
        "cadence": "daily",   # change to "weekly"/"both" here anytime
        "url": "https://e1-vhp.com/fr/report/yearly-forecast-of-room-occupancy",
        # Dashboard import: section B (monthly ACTUALS only; never budget/LY).
        "section": "B",
        "source": "vhp:yearly-forecast",
        # The report shows 12 months FROM its start month (an Element-UI month
        # picker). "calendar_year" sets the start to JANUARY of the current year,
        # so the export is always a clean Jan..Dec of this year — closed months
        # are actuals, the current month updates daily, later months are forecast.
        # (VHP's own default starts at the CURRENT month, which would put next
        # year's forecast into Jan..Sep — so we set January explicitly.)
        "period": "calendar_year",
        # Gate ON: if the January start can't be set AND verified, SKIP rather
        # than upload the wrong (current-month-start) export.
        "period_required": True,
        # Filter settings applied before SEARCH (best-effort; see vhp.configure_report):
        "checkboxes": {"Show Revenue": True},
        # Exact VHP option labels to turn OFF in the Segment Code list.
        "segment_untick": ["COMPLIMENT", "HOUSE USE"],
    },
    {
        "name": "reservation-by-creation-date",
        "cadence": "weekly",
        "url": "https://e1-vhp.com/fr/reservation-by-creation-date",
        # Weekly run is scheduled for FRIDAY. This selects last Friday .. this
        # Thursday — the 7 full days ending the day before the run (yesterday).
        # (days_back=7, include_today=False -> today-7 .. today-1.)
        "period": {"days_back": 7, "include_today": False},
        # Dashboard import: section C (full replace of that week's segment rows).
        "section": "C",
        "source": "vhp:reservation-by-creation-date",
        # Verified working (V-Calendar date picker). Gate ON: if the date range
        # can't be set AND verified, SKIP this report rather than upload a CSV
        # with VHP's default (wrong) window.
        "period_required": True,
    },
]

# ---------------------------------------------------------------------------
# UPLOAD
# ---------------------------------------------------------------------------
# The scraper POSTs each downloaded file to the dashboard, which runs its
# existing CSV/XLSX column mapping and writes to Postgres.
#
# TODO: confirm the exact endpoint path + form field name from the dashboard
# repo. If the endpoint needs auth, put the bearer token in the VHP_UPLOAD_TOKEN
# environment variable (preferred) or paste it into `token` below.
# Dashboard import API (see VHP_INTEGRATION.md). The scraper POSTs each file to
# /api/import with property + section + week_ending; the dashboard's AI extracts
# it. Put the shared secret in the IMPORT_API_TOKEN environment variable — never
# commit it. (uploader.py sends field name "file" and hard-codes the contract.)
UPLOAD = {
    "url": "https://dashboard.bluekarmasecrets.com/api/import",
    "token": IMPORT_API_TOKEN,
    "timeout": 180,
    "retry_backoffs": [2, 4, 8, 16],  # seconds; retried only on network / 429 / 5xx
}

# ---------------------------------------------------------------------------
# JOBS — on-demand scraping requested from the dashboard (optional feature)
# ---------------------------------------------------------------------------
# When enabled, poller.py runs continuously on this PC and asks the dashboard
# for queued scrape requests (the dashboard can't reach IN to this office PC, so
# the PC reaches OUT and polls). For each job it scrapes the requested period and
# uploads the result through the same /api/import endpoint, then reports the job
# done/failed. See VHP_JOBS_INTEGRATION.md for the full contract (and the
# dashboard-side endpoints your dashboard agent builds).
#
# Set "enabled": False to turn the whole feature off (the scheduled daily/weekly
# runs are unaffected — they don't use any of this).
JOBS = {
    "enabled": True,
    # Scraper -> dashboard endpoints (built on the dashboard per the spec):
    #   claim_url  : POST -> hands back the oldest queued job (or none), atomically
    #   result_url : POST -> records a job's outcome ({id} is replaced per job)
    "claim_url": "https://dashboard.bluekarmasecrets.com/api/jobs/claim",
    "result_url": "https://dashboard.bluekarmasecrets.com/api/jobs/{id}/result",
    # Reuse the upload token by default (same shared secret). Set a separate
    # VHP_JOBS_TOKEN env var if you'd rather the queue use its own token.
    "token": os.environ.get("VHP_JOBS_TOKEN", "") or IMPORT_API_TOKEN,
    # A name for THIS PC in the dashboard's job records (who claimed the job).
    "worker": os.environ.get("VHP_WORKER_ID", "") or _HOSTNAME or "vhp-pc",
    "poll_interval": 120,   # seconds to wait between polls when the queue is empty
    "timeout": 60,          # HTTP timeout (seconds) for claim/result calls
    "retry_backoffs": [2, 4, 8, 16],  # retry claim/result on network / 429 / 5xx
}

# Lock file that serialises Chrome access so an on-demand job (poller.py) and the
# scheduled run (main.py) never overlap and kill each other's chromedriver.
RUN_LOCK_PATH = os.path.join(BASE, "vhp-run.lock")

# Export file extension VHP's "Print CSV" produces (used when renaming).
EXPORT_EXT = "csv"

# How long to wait (seconds) for a download to finish before giving up.
DOWNLOAD_TIMEOUT = 120

# Folders to watch for a finished download. We prefer our own downloads/ dir,
# but Chrome (attached to an existing window) can land files in the user's
# default Downloads after a page navigation — so watch both.
DOWNLOAD_WATCH = [
    DOWNLOADS_DIR,
    os.path.join(os.path.expanduser("~"), "Downloads"),
]

# Optional failure alerting. Leave webhook empty to disable Slack alerts.
ALERTS = {
    "slack_webhook": os.environ.get("VHP_SLACK_WEBHOOK", ""),
}

# ---------------------------------------------------------------------------
# CREDENTIALS
# ---------------------------------------------------------------------------
# Per-property VHP logins. Create credentials.py (copy credentials.example.py)
# and fill it in — it is git-ignored, so passwords never get committed.
# Environment variables VHP_<CODE>_USER / VHP_<CODE>_PASS override the file.
try:
    from credentials import CREDENTIALS as _CREDS
except Exception:  # file not created yet
    _CREDS = {}


def report_by_section(section):
    """Return the REPORTS entry whose dashboard section matches (e.g. 'B' or 'C'),
    or None. Used by poller.py to map an on-demand job's section to the report
    definition it should scrape. Case-insensitive."""
    want = (section or "").strip().upper()
    for r in REPORTS:
        if (r.get("section") or "").strip().upper() == want:
            return r
    return None


def get_credentials(code):
    """Return (username, password) for a property code, or None if unset."""
    entry = _CREDS.get(code, {})
    user = entry.get("username") or os.environ.get("VHP_{}_USER".format(code.upper()), "")
    pwd = entry.get("password") or os.environ.get("VHP_{}_PASS".format(code.upper()), "")
    if user and pwd:
        return user, pwd
    return None
