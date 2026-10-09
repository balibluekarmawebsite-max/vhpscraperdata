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
