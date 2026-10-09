"""
config.py — the ONE place you edit for day-to-day setup.

Everything the scraper needs to know about *your* VHP + dashboard lives here:
which properties to log into, which reports to pull, and where to upload them.

Nothing in the other files should need editing for normal operation.
"""
import os

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
    {
        "code": "bkds",
        "name": "Blue Karma Dijiwa Seminyak",
        "profile": os.path.join(PROFILES_DIR, "bkds"),
        "home_url": "https://e1-vhp.com/login",
    },
    {
        "code": "bkdu",
        "name": "Blue Karma Dijiwa Ubud",
        "profile": os.path.join(PROFILES_DIR, "bkdu"),
        "home_url": "https://e1-vhp.com/login",
    },
    {
        "code": "bkv",
        "name": "Blue Karma Village",
        "profile": os.path.join(PROFILES_DIR, "bkv"),
        "home_url": "https://e1-vhp.com/login",
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
#                "current_month" -> set the Month box to this month
#                "last_7_days"   -> set the Date range to the previous 7 days
#                None            -> leave VHP's default, just press SEARCH
REPORTS = [
    {
        "name": "yearly-forecast-of-room-occupancy",
        "cadence": "daily",   # change to "weekly"/"both" here anytime
        "url": "https://e1-vhp.com/fr/report/yearly-forecast-of-room-occupancy",
        "period": "current_month",
    },
    {
        "name": "reservation-by-creation-date",
        "cadence": "weekly",
        "url": "https://e1-vhp.com/fr/reservation-by-creation-date",
        "period": "last_7_days",
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
UPLOAD = {
    "url": "https://analytics.bluekarmasecrets.com/api/upload",
    "field": "file",
    "token": os.environ.get("VHP_UPLOAD_TOKEN", ""),
    "timeout": 120,
}

# Export file extension VHP's "Print CSV" produces (used when renaming).
EXPORT_EXT = "csv"

# How long to wait (seconds) for a download to finish before giving up.
DOWNLOAD_TIMEOUT = 120

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


def get_credentials(code):
    """Return (username, password) for a property code, or None if unset."""
    entry = _CREDS.get(code, {})
    user = entry.get("username") or os.environ.get("VHP_{}_USER".format(code.upper()), "")
    pwd = entry.get("password") or os.environ.get("VHP_{}_PASS".format(code.upper()), "")
    if user and pwd:
        return user, pwd
    return None
