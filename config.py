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
    {
        "code": "bkds",
        "name": "Blue Karma Dijiwa Seminyak",
        "profile": os.path.join(PROFILES_DIR, "bkds"),
        "home_url": "PASTE-BKDS-VHP-URL",
    },
    {
        "code": "bkdu",
        "name": "Blue Karma Dijiwa Ubud",
        "profile": os.path.join(PROFILES_DIR, "bkdu"),
        "home_url": "PASTE-BKDU-VHP-URL",
    },
    {
        "code": "bkv",
        "name": "Blue Karma Village",
        "profile": os.path.join(PROFILES_DIR, "bkv"),
        "home_url": "PASTE-BKV-VHP-URL",
    },
]

# ---------------------------------------------------------------------------
# REPORTS
# ---------------------------------------------------------------------------
# Defined ONCE and reused for every property (same VHP UI across properties).
#
# Fields:
#   name          : short slug used in the saved filename and upload tag.
#   cadence       : "daily", "weekly", or "both" — controls which run picks it up.
#   url           : deep-link to the report if VHP has one; else None -> use
#                   the recorded navigation clicks in vhp.py.
#   export_label  : visible text of the export/download button to click.
#   date_range    : how far back to pull. "yesterday" | "last_7_days" | None.
#                   (Interpreted in vhp.py::set_date_range — adapt to your UI.)
#
# TODO: replace these two examples with the exact reports you need.
REPORTS = [
    {
        "name": "daily-revenue",
        "cadence": "daily",
        "url": None,
        "export_label": "Export",
        "date_range": "yesterday",
    },
    {
        "name": "weekly-market-segment",
        "cadence": "weekly",
        "url": None,
        "export_label": "Export",
        "date_range": "last_7_days",
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

# Export file extension VHP produces (used when renaming the download).
# "xlsx" or "csv".
EXPORT_EXT = "xlsx"

# How long to wait (seconds) for a download to finish before giving up.
DOWNLOAD_TIMEOUT = 120

# Optional failure alerting. Leave webhook empty to disable Slack alerts.
ALERTS = {
    "slack_webhook": os.environ.get("VHP_SLACK_WEBHOOK", ""),
}
