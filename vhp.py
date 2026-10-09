"""
vhp.py — drive one e1-vhp.com report and capture its CSV.

Flow per report (same for every report — all use VHP's built-in "Print CSV"):
    open the report URL
      -> (use VHP's default period)         set_period()
      -> click the blue SEARCH button        click_search()
      -> wait for the data grid to populate   wait_for_report_data()
      -> click the printer icon -> "Print CSV"  export_print_csv()
      -> Chrome downloads a .csv              wait_for_download()
      -> rename it <code>-<report>-<date>.csv rename_download()

e1-vhp.com is a Vue/Quasar single-page app, so report data loads asynchronously
after SEARCH — every step waits explicitly rather than assuming instant render.
"""
import datetime
import os
import time

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import config


class LoginRequired(Exception):
    """Raised when VHP shows the login form instead of the report (the saved
    session has expired / this property is not logged in)."""


# --- Locators (built from the live DOM; Quasar "q-" classes) ----------------

# A to-Z translate pair makes text matches case-insensitive in XPath 1.0.
_LOWER = "abcdefghijklmnopqrstuvwxyz"
_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _ci(expr):
    """Wrap an XPath string expression so it compares upper-cased."""
    return "translate(normalize-space({}),'{}','{}')".format(expr, _LOWER, _UPPER)


# The blue "SEARCH" button in the left filter panel (has visible text).
SEARCH_BTN = "//button[contains({}, 'SEARCH')]".format(_ci("."))

# The "Print CSV" entry in the menu opened by the printer icon.
PRINT_CSV_ITEM = (
    "//*[contains(@class,'q-item')][contains({ci}, 'PRINT CSV')]"
    " | //*[{ci_text} = 'PRINT CSV']"
).format(
    ci=_ci("."),
    ci_text="translate(normalize-space(text()),'{}','{}')".format(_LOWER, _UPPER),
)

# A login password field = we are NOT logged in.
_LOGIN_FIELDS = [
    "input[type='password']",
    "input[data-cy='password-input-login']",
    "input[data-cy='username-input-login']",
]


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _safe_click(driver, el):
    """Click, falling back to a JS click if the normal one is intercepted."""
    try:
        el.click()
    except Exception:  # noqa: BLE001
        driver.execute_script("arguments[0].click();", el)


def _toolbar_buttons(driver):
    """The round icon buttons (refresh, print) that sit just above the report
    table. In document order the printer is the LAST one before the table."""
    return driver.find_elements(
        By.XPATH,
        "(//div[contains(@class,'q-table__container')])[1]"
        "/preceding::button[contains(@class,'q-btn--round')]",
    )


# ---------------------------------------------------------------------------
# login detection
# ---------------------------------------------------------------------------
def is_logged_in(driver):
    """True unless a visible login field is on screen."""
    time.sleep(1)  # let the SPA settle after navigation
    for sel in _LOGIN_FIELDS:
        for el in driver.find_elements(By.CSS_SELECTOR, sel):
            if el.is_displayed():
                return False
    return True


# ---------------------------------------------------------------------------
# per-report steps
# ---------------------------------------------------------------------------
def open_report(driver, report):
    """Deep-link to the report and wait for it to render.

    Detects login state at the REAL report page (not the /login route, which
    always shows a form): if the SEARCH button appears we're in; if a login
    field appears we were bounced to login -> raise LoginRequired.
    """
    driver.get(report["url"])
    end = time.time() + 40
    while time.time() < end:
        if driver.find_elements(By.XPATH, SEARCH_BTN):
            return  # report rendered -> logged in
        for sel in _LOGIN_FIELDS:
            if any(e.is_displayed() for e in driver.find_elements(By.CSS_SELECTOR, sel)):
                raise LoginRequired(
                    "bounced to login on report {!r}".format(report["name"])
                )
        time.sleep(0.5)
    raise TimeoutError("report page did not render: {}".format(report["url"]))


def set_period(driver, report):
    """Set the report's date filter before searching.

    v1 uses VHP's DEFAULT period, which already matches a daily/weekly run:
      * "current_month" reports (e.g. the forecast) open on the current month.
      * "last_7_days" reports (e.g. reservations) open on the recent window.
    Setting an exact custom range (typing into VHP's Month / Date controls) is a
    planned refinement; it needs per-report field locators. For now we leave the
    default and just press SEARCH.  (report["period"] documents the intent.)
    """
    return


def click_search(driver):
    btn = WebDriverWait(driver, 30).until(
        EC.element_to_be_clickable((By.XPATH, SEARCH_BTN))
    )
    _safe_click(driver, btn)


def wait_for_report_data(driver, timeout=60):
    """Wait until the Quasar table has real cells (data loaded via XHR)."""
    end = time.time() + timeout
    while time.time() < end:
        cells = driver.find_elements(By.CSS_SELECTOR, "table.q-table td")
        if sum(1 for c in cells if c.text.strip()) >= 3:
            return
        time.sleep(1)
    raise TimeoutError("report data did not load after SEARCH")


def export_print_csv(driver):
    """Open the printer menu and click 'Print CSV' to trigger the download."""
    buttons = _toolbar_buttons(driver)
    if not buttons:
        raise RuntimeError("report toolbar (refresh/print icons) not found")
    _safe_click(driver, buttons[-1])  # printer is the right-most toolbar icon
    item = WebDriverWait(driver, 15).until(
        EC.element_to_be_clickable((By.XPATH, PRINT_CSV_ITEM))
    )
    _safe_click(driver, item)


# ---------------------------------------------------------------------------
# download capture
# ---------------------------------------------------------------------------
def wait_for_download(folder, timeout=None):
    """Return the path of the newly downloaded file (Chrome writes a temporary
    .crdownload until the download finishes)."""
    if timeout is None:
        timeout = config.DOWNLOAD_TIMEOUT
    os.makedirs(folder, exist_ok=True)
    before = set(os.listdir(folder))
    end = time.time() + timeout
    while time.time() < end:
        new_files = set(os.listdir(folder)) - before
        done = [
            f
            for f in new_files
            if not f.endswith(".crdownload") and not f.endswith(".tmp")
        ]
        if done:
            done.sort(
                key=lambda f: os.path.getmtime(os.path.join(folder, f)),
                reverse=True,
            )
            return os.path.join(folder, done[0])
        time.sleep(1)
    raise TimeoutError("download did not finish within {}s".format(timeout))


def _unique(path):
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 1
    while os.path.exists("{}-{}{}".format(root, i, ext)):
        i += 1
    return "{}-{}{}".format(root, i, ext)


def rename_download(src, prop_code, report_name, folder):
    """Rename the raw download to <code>-<report>-<YYYY-MM-DD>.<ext>."""
    date = datetime.date.today().strftime("%Y-%m-%d")
    ext = os.path.splitext(src)[1].lstrip(".") or config.EXPORT_EXT
    dest = os.path.join(
        folder, "{}-{}-{}.{}".format(prop_code, report_name, date, ext)
    )
    dest = _unique(dest)
    os.replace(src, dest)
    return dest


# ---------------------------------------------------------------------------
# orchestration for one report
# ---------------------------------------------------------------------------
def scrape_report(driver, prop, report, download_dir):
    """Run the full flow for one report; return the saved CSV path.

    Raises on failure so main.py can log it and carry on with the next report.
    """
    open_report(driver, report)
    set_period(driver, report)
    click_search(driver)
    wait_for_report_data(driver)
    time.sleep(1)  # let the grid settle before exporting
    export_print_csv(driver)
    raw = wait_for_download(download_dir)
    return rename_download(raw, prop["code"], report["name"], download_dir)
