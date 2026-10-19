"""
browser.py — builds the Selenium Chrome driver for Windows 7 / Chrome 109.

Critical rules (see README "Hard constraints"):
  * We pass an explicit Service(executable_path=chromedriver.exe) so Selenium
    Manager never auto-fetches a newer, Win7-incompatible driver.
  * Each property gets its own --user-data-dir so logins stay isolated and
    persistent across runs.
  * Headed (visible) Chrome — VHP + Win7 behave better visible than headless.
"""
import os

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

import config


def make_driver(profile_dir, download_dir, headless=False):
    """Return a configured Chrome WebDriver.

    profile_dir   : Chrome --user-data-dir for this property (persists login).
    download_dir  : where exported files should land.
    headless      : leave False on Win7/VHP; True is only for debugging.
    """
    os.makedirs(profile_dir, exist_ok=True)
    os.makedirs(download_dir, exist_ok=True)

    opts = Options()
    opts.add_argument("--user-data-dir={}".format(profile_dir))

    # Keep Chrome 109 from fighting us on an old/locked-down machine.
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-background-networking")
    # Chrome component/auto updates off (belt-and-braces; also disable the
    # Google Update service at the OS level per README).
    opts.add_argument("--disable-component-update")
    # Quiet the harmless GLES/EGL fallback noise from the old Intel GPU on
    # Win7 so logs/run.log stays readable (software rendering is fine here).
    opts.add_argument("--log-level=3")
    opts.add_argument("--disable-gpu")
    opts.add_experimental_option("excludeSwitches", ["enable-logging"])

    if headless:
        opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1600,1000")

    opts.add_experimental_option(
        "prefs",
        {
            "download.default_directory": download_dir,
            "download.prompt_for_download": False,  # no Save-As dialog
            "download.directory_upgrade": True,
            "safebrowsing.enabled": True,
            # Allow automatic multiple downloads without a prompt.
            "profile.default_content_setting_values.automatic_downloads": 1,
        },
    )

    if not os.path.exists(config.CHROMEDRIVER_PATH):
        raise FileNotFoundError(
            "ChromeDriver not found at {}. Download ChromeDriver 109 and place "
            "it there (see README).".format(config.CHROMEDRIVER_PATH)
        )

    service = Service(executable_path=config.CHROMEDRIVER_PATH)
    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(60)
    return driver


def _ensure_min_width(driver, min_width):
    """Ensure the attached window is at least `min_width` px wide, WITHOUT
    activating it (no focus steal, no pop-to-front). Uses CDP setWindowBounds,
    which only changes geometry. A window the user already keeps wide or
    maximized is left completely untouched — so the bot stays in the background
    and never interrupts other work on the PC."""
    try:
        info = driver.execute_cdp_cmd("Browser.getWindowForTarget", {})
        bounds = info.get("bounds", {}) or {}
        state = bounds.get("windowState", "normal")
        if state in ("maximized", "fullscreen"):
            return  # already wide; leave it exactly as the user has it
        if (bounds.get("width") or 0) >= min_width:
            return  # already wide enough
        driver.execute_cdp_cmd("Browser.setWindowBounds", {
            "windowId": info.get("windowId"),
            "bounds": {"width": 1600, "height": 1000, "windowState": "normal"},
        })
    except Exception:  # noqa: BLE001 - a resize hiccup must never break the attach
        pass


def kill_stale_chromedrivers():
    """Windows: end leftover chromedriver.exe from previous attached runs.
    Does NOT touch chrome.exe, so the user's open VHP windows are untouched."""
    if os.name == "nt":
        os.system("taskkill /F /IM chromedriver.exe >nul 2>&1")


def make_driver_attached(debug_port, download_dir):
    """Attach to an ALREADY-RUNNING Chrome that the user opened with
    --remote-debugging-port=<debug_port> (via open-<code>.bat) and logged into
    VHP by hand. We drive that window; we never launch or log in ourselves, so
    VHP sees a normal, human-logged-in browser (no CAPTCHA, no 'Not Allowed').

    IMPORTANT: do NOT call driver.quit() on the result — that would close the
    user's window. The caller just stops using it and leaves it open.
    """
    if not os.path.exists(config.CHROMEDRIVER_PATH):
        raise FileNotFoundError(
            "ChromeDriver not found at {}".format(config.CHROMEDRIVER_PATH)
        )
    opts = Options()
    opts.add_experimental_option("debuggerAddress", "127.0.0.1:{}".format(int(debug_port)))

    service = Service(executable_path=config.CHROMEDRIVER_PATH)
    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(60)

    # Make sure the window is wide enough for the filter panel, but WITHOUT
    # stealing focus or popping it to the foreground — so the bot never disturbs
    # whatever else you're doing on the PC. (We used to maximize on every attach,
    # which could pull windows forward every few minutes.)
    _ensure_min_width(driver, 1280)

    # Send downloads to our folder for this session (the window's own default
    # download dir is left unchanged).
    os.makedirs(download_dir, exist_ok=True)
    for cmd in ("Page.setDownloadBehavior", "Browser.setDownloadBehavior"):
        try:
            driver.execute_cdp_cmd(cmd, {"behavior": "allow", "downloadPath": download_dir})
            break
        except Exception:  # noqa: BLE001 - try the next CDP command name
            continue
    return driver
