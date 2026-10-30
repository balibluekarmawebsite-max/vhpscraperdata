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
import socket
import subprocess
import time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

import config


class WindowLaunchError(Exception):
    """A property's Chrome window was closed and could not be re-opened
    (chrome.exe not found, or the debug port never came up after launch)."""


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


# ---------------------------------------------------------------------------
# Auto-reopen a CLOSED window (self-heal after a PC restart / Chrome crash)
# ---------------------------------------------------------------------------
_CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def find_chrome():
    """Locate chrome.exe: config.CHROME_PATH / CHROME_PATH env first, then the usual
    install paths. Returns the path, or None if not found."""
    cands = []
    cfg = getattr(config, "CHROME_PATH", "") or os.environ.get("CHROME_PATH", "")
    if cfg:
        cands.append(cfg)
    cands.extend(_CHROME_CANDIDATES)
    for c in cands:
        try:
            if c and os.path.isfile(c):
                return c
        except OSError:
            continue
    return None


def _port_open(port, timeout=1.0):
    """True if something is listening on 127.0.0.1:port (the debug port is up)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", int(port))) == 0
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def _kill_chrome_for_port(port):
    """Kill ONLY the scraper's own Chrome for this debug port — a hung instance that
    still holds the profile so a fresh launch can't open the port. Matches on
    '--remote-debugging-port=<port>', which a normal/ personal Chrome never has, so
    this can never close the user's own browsing. Best-effort, Windows-only."""
    if os.name != "nt":
        return
    needle = "--remote-debugging-port={}".format(int(port))
    try:
        out = subprocess.check_output(
            'wmic process where "name=\'chrome.exe\'" get ProcessId,CommandLine '
            '/format:list', shell=True, stderr=subprocess.DEVNULL,
            universal_newlines=True)
    except Exception:  # noqa: BLE001 - wmic missing/failed -> skip (just launch)
        return
    want = False  # /format:list prints CommandLine= then ProcessId= per process
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("CommandLine="):
            # Exact-token match (not a substring) so port 9222 never matches a
            # cmdline that uses e.g. 92220; the user's own Chrome has no such flag.
            want = needle in line[len("CommandLine="):].split()
        elif line.startswith("ProcessId=") and want:
            pid = line[len("ProcessId="):].strip()
            if pid.isdigit():
                try:
                    subprocess.call("taskkill /F /PID {} >nul 2>&1".format(pid),
                                    shell=True)
                except Exception:  # noqa: BLE001
                    pass
            want = False


def launch_chrome(prop, logger=None):
    """(Re)launch the property's Chrome window — detached, same flags as
    open-<code>.bat, on the VHP login page. Returns True if the launch was issued
    (the caller then waits for the port + attaches). Never raises."""
    chrome = find_chrome()
    if not chrome:
        if logger:
            logger.error("[%s] cannot auto-reopen: chrome.exe not found "
                         "(set the CHROME_PATH env var)", prop.get("code"))
        return False
    try:
        os.makedirs(prop["profile"], exist_ok=True)
    except OSError:
        pass
    flags = " ".join(getattr(config, "CHROME_LAUNCH_FLAGS", []) or [])
    url = prop.get("home_url") or "https://e1-vhp.com/login"
    # cmd's START fully detaches the window so it survives this process exiting
    # (exactly like open-<code>.bat). Quote the exe / profile / url for spaces.
    cmd = ('start "" "{chrome}" --remote-debugging-port={port} '
           '--user-data-dir="{profile}" {flags} "{url}"').format(
        chrome=chrome, port=int(prop["debug_port"]), profile=prop["profile"],
        flags=flags, url=url)
    try:
        subprocess.Popen(cmd, shell=True, close_fds=True)
        return True
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.error("[%s] failed to launch Chrome: %s", prop.get("code"), exc)
        return False


def ensure_window(prop, download_dir, logger=None, launch_wait=35):
    """Attach to the property's Chrome window, RE-LAUNCHING it if it's closed.

    * If the window is open, attach and return the driver (the normal flow then
      handles login, respecting the human-presence rules).
    * If the window is CLOSED/unreachable, kill any hung Chrome still holding its
      debug port, launch a fresh window (detached, on the login page), wait for the
      port, attach, and — since the bot itself just opened it, so no human is on it
      — try the opt-in auto-login once. Returns a driver that is logged in (if
      auto-login succeeded) or sitting on the login page (for a human / the next
      keep-alive tick).

    Raises WindowLaunchError if the window can't be brought up (chrome.exe missing,
    or the port never comes up), so the caller logs it and moves on to the next
    property."""
    try:
        return make_driver_attached(prop["debug_port"], download_dir)
    except Exception:  # noqa: BLE001 - window not open / not reachable -> re-launch
        pass

    if logger:
        logger.warning("[%s] window on port %s is closed — auto-reopening it",
                        prop.get("code"), prop.get("debug_port"))
    _kill_chrome_for_port(prop["debug_port"])
    if not launch_chrome(prop, logger):
        raise WindowLaunchError(
            "could not auto-reopen the {} window (chrome.exe not found?)".format(
                prop.get("code")))

    deadline = time.time() + launch_wait
    last = None
    driver = None
    while time.time() < deadline:
        if _port_open(prop["debug_port"]):
            kill_stale_chromedrivers()  # clear any failed-attach chromedrivers
            try:
                driver = make_driver_attached(prop["debug_port"], download_dir)
                break
            except Exception as exc:  # noqa: BLE001 - port up but attach raced; retry
                last = exc
        time.sleep(1.5)
    if driver is None:
        raise WindowLaunchError(
            "auto-reopened the {} window but its debug port {} never came up "
            "({})".format(prop.get("code"), prop.get("debug_port"), last))

    if logger:
        logger.info("[%s] window re-opened; attempting auto-login", prop.get("code"))
    # Fresh, bot-opened window -> definitely no human on it -> auto-login is safe.
    try:
        import vhp  # lazy import avoids any import-order coupling
        vhp.attempt_auto_login(driver, prop, recovery=True, logger=logger)
    except Exception as exc:  # noqa: BLE001 - best-effort; caller re-checks login
        if logger:
            logger.info("[%s] post-reopen auto-login not completed: %s",
                        prop.get("code"), exc)
    return driver
