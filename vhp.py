"""
vhp.py — navigate to a report, set the date range, export, wait for the
download, and rename the file.

HOW TO FILL THIS IN
-------------------
VHP's exact buttons/links differ per install, so the navigation and date-range
steps below are TEMPLATES with TODO markers. Record your clicks with the
Selenium IDE browser extension (or UI.Vision), then translate them here.

Prefer STABLE locators — visible link text / button labels (By.LINK_TEXT,
By.PARTIAL_LINK_TEXT, By.XPATH on text()) — over brittle auto-generated
absolute XPaths, which are the usual reason an automation silently breaks.
"""
import datetime
import os
import time

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import config


# ---------------------------------------------------------------------------
# Login detection
# ---------------------------------------------------------------------------
def is_logged_in(driver):
    """Heuristic: if a password field is visible, we're on a login screen.

    Adapt the locator if VHP's login page uses something other than a plain
    <input type="password">.
    """
    pwd_fields = driver.find_elements(By.CSS_SELECTOR, "input[type='password']")
    return not any(f.is_displayed() for f in pwd_fields)


# ---------------------------------------------------------------------------
# Navigation to a report
# ---------------------------------------------------------------------------
def open_report(driver, prop, report):
    """Open a report page: deep-link if the report has a URL, else click
    through the menus.

    TODO: replace the click-through branch with your recorded navigation.
    """
    if report.get("url"):
        driver.get(report["url"])
        return

    # --- recorded navigation (EXAMPLE — replace with your real clicks) ---
    # e.g. Reports menu -> the specific report link, matched by visible text.
    wait = WebDriverWait(driver, 30)
    # wait.until(EC.element_to_be_clickable((By.LINK_TEXT, "Reports"))).click()
    # wait.until(
    #     EC.element_to_be_clickable((By.PARTIAL_LINK_TEXT, report["name"]))
    # ).click()
    raise NotImplementedError(
        "Navigation for report '{}' is not recorded yet. Either set its 'url' "
        "in config.py or fill in open_report() in vhp.py.".format(report["name"])
    )


# ---------------------------------------------------------------------------
# Date range
# ---------------------------------------------------------------------------
def set_date_range(driver, report):
    """Set the report's date range before exporting.

    date_range values are interpreted here; adapt to how VHP's UI accepts
    dates (typed field, date-picker, or URL param).

    TODO: implement against your VHP date controls.
    """
    kind = report.get("date_range")
    if not kind:
        return

    today = datetime.date.today()
    if kind == "yesterday":
        start = end = today - datetime.timedelta(days=1)
    elif kind == "last_7_days":
        end = today - datetime.timedelta(days=1)
        start = end - datetime.timedelta(days=6)
    else:
        # Unknown token — leave the UI at its default and let the caller log it.
        return

    _ = (start, end)  # noqa: F841  (wire these into the date fields below)
    # EXAMPLE (replace locators/format with your UI):
    # fmt = "%Y-%m-%d"
    # from_field = driver.find_element(By.ID, "dateFrom")
    # from_field.clear(); from_field.send_keys(start.strftime(fmt))
    # to_field = driver.find_element(By.ID, "dateTo")
    # to_field.clear(); to_field.send_keys(end.strftime(fmt))


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def click_export(driver, report):
    """Click the export/download button, matched by its visible label."""
    label = report.get("export_label", "Export")
    wait = WebDriverWait(driver, 30)
    btn = wait.until(
        EC.element_to_be_clickable(
            (By.XPATH, "//*[self::button or self::a][contains(normalize-space(.), "
                       "{!r})]".format(label))
        )
    )
    btn.click()


# ---------------------------------------------------------------------------
# Wait for download
# ---------------------------------------------------------------------------
def wait_for_download(folder, timeout=None):
    """Return the path of the newly downloaded file.

    Chrome writes a `.crdownload` temp file until the download finishes, so we
    wait for a new, non-.crdownload file to appear in `folder`.
    """
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
            # Newest, in case several appeared.
            done.sort(
                key=lambda f: os.path.getmtime(os.path.join(folder, f)),
                reverse=True,
            )
            return os.path.join(folder, done[0])
        time.sleep(1)
    raise TimeoutError("download did not finish within {}s".format(timeout))


def _unique(path):
    """If path exists, append -1, -2, ... before the extension."""
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
# Orchestration for one report
# ---------------------------------------------------------------------------
def scrape_report(driver, prop, report, download_dir):
    """Run the full flow for one report and return the saved file path.

    Raises on failure so the caller (main.py) can log and continue.
    """
    open_report(driver, prop, report)
    set_date_range(driver, report)
    click_export(driver, report)
    raw = wait_for_download(download_dir)
    return rename_download(raw, prop["code"], report["name"], download_dir)
