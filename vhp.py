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
import json
import os
import re
import time

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
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

# XPaths that enumerate the rows of an open Quasar menu/dropdown. We try them
# in order and use the first that yields visible rows: scoped to the floating
# .q-menu first (the printer dropdown lives there), then a broad fallback.
_MENU_ITEM_XPATHS = [
    "//div[contains(@class,'q-menu')]//div[contains(@class,'q-item')]",
    "//div[contains(@class,'q-menu')]//*[@role='menuitem' or contains(@class,'q-item')]",
    "//*[contains(@class,'q-item')]",
]

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


_FULL_CLICK_JS = (
    "var e=arguments[0];"
    "['mouseover','mousedown','mouseup','click'].forEach(function(t){"
    "try{e.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window}));}"
    "catch(_){}});"
)


def _force_click(driver, el):
    """Click that still fires when an overlay/backdrop intercepts a normal click:
    native click, then a JS .click(), then a full mouse-event sequence."""
    try:
        el.click()
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        driver.execute_script("arguments[0].click();", el)
    except Exception:  # noqa: BLE001
        pass
    try:
        driver.execute_script(_FULL_CLICK_JS, el)
    except Exception:  # noqa: BLE001
        pass


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
def _session_expired(driver):
    """True if VHP is showing its 'Your Session has been expired, please re-login'
    dialog. The session timed out server-side, so a human must re-login — we treat
    this exactly like a login form (the scraper can't get past VHP's CAPTCHA)."""
    xp = ("//*[contains({c}, 'SESSION') and contains({c}, 'EXPIRED')]"
          " | //*[contains({c}, 'RE-LOGIN')]"
          " | //*[contains({c}, 'RELOGIN')]").format(c=_ci("."))
    for el in driver.find_elements(By.XPATH, xp):
        try:
            if el.is_displayed():
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


# Dialog/message-box containers that could hold VHP's 'session expired' alert.
# The app mixes Quasar (q-dialog) and Element-UI (el-message-box), so we match
# both rather than assuming one framework.
_DIALOG_CONTAINER_XPATH = (
    "//div[contains(@class,'q-dialog') or contains(@class,'el-message-box') "
    "or contains(@class,'q-card') or @role='dialog' or @role='alertdialog']")
# An OK / confirm button *inside* a given container element.
_OK_IN_CONTAINER_XPATH = (
    ".//button[normalize-space(.)='OK' or .//span[normalize-space(.)='OK'] "
    "or contains(concat(' ',normalize-space(@class),' '),' el-button--primary ')]")
# Last-resort: any visible OK/confirm button (only used if we can't pin the
# container). Still gated on _session_expired() being true first.
_OK_ANYWHERE_XPATH = (
    "//button[normalize-space(.)='OK' or .//span[normalize-space(.)='OK'] "
    "or contains(concat(' ',normalize-space(@class),' '),' el-button--primary ')]")


def _expired_container(driver):
    """Return the dialog/message-box element that actually shows the 'session
    expired' text, so the OK we click belongs to IT — not some stale OK button left
    over elsewhere on the page. None if we can't pin it."""
    for cont in driver.find_elements(By.XPATH, _DIALOG_CONTAINER_XPATH):
        try:
            if not cont.is_displayed():
                continue
            txt = (cont.text or "").upper()
            if ("SESSION" in txt and "EXPIRED" in txt) or "RE-LOGIN" in txt or "RELOGIN" in txt:
                return cont
        except Exception:  # noqa: BLE001 - stale element, keep looking
            continue
    return None


def dismiss_session_expired(driver):
    """Click OK on VHP's 'Your Session has been expired' dialog, if it's up.

    This is the first manual step after a timeout: click OK, and VHP drops you on
    the /login page with the username + password already filled, needing only the
    CAPTCHA. The bot can safely do THIS part — clicking OK just closes the modal,
    it does NOT reload the page or generate a new CAPTCHA — so the window is left
    sitting ready on the login page.

    Returns True ONLY if the dialog is confirmed GONE afterwards (so callers never
    tell the human 'ready on the login page' when the dialog is in fact still up).
    No-op (returns False) when the dialog isn't showing, so it's safe to call
    unconditionally."""
    if not _session_expired(driver):
        return False
    cont = _expired_container(driver)
    buttons = []
    if cont is not None:
        try:
            buttons = cont.find_elements(By.XPATH, _OK_IN_CONTAINER_XPATH)
        except Exception:  # noqa: BLE001
            buttons = []
    if not buttons:
        buttons = driver.find_elements(By.XPATH, _OK_ANYWHERE_XPATH)
    for el in buttons:
        try:
            if not el.is_displayed():
                continue
            _force_click(driver, el)  # fires even behind the dialog backdrop
            time.sleep(1)  # let the modal close / redirect to /login
            if not _session_expired(driver):
                return True  # confirmed closed
        except Exception:  # noqa: BLE001 - stale element, try the next match
            continue
    return not _session_expired(driver)  # True only if it actually closed


def is_logged_in(driver):
    """True unless a visible login field OR the 'session expired' dialog is on screen."""
    time.sleep(1)  # let the SPA settle after navigation
    if _session_expired(driver):
        return False
    for sel in _LOGIN_FIELDS:
        for el in driver.find_elements(By.CSS_SELECTOR, sel):
            if el.is_displayed():
                return False
    return True


# ---------------------------------------------------------------------------
# automatic login
# ---------------------------------------------------------------------------
_USER_SELECTORS = [
    "input[data-cy='username-input-login']",
    "input[type='email']",
    "input[name='username']",
    "input[name='user']",
    "input[name='email']",
    "input[type='text']",
]
_PASS_SELECTORS = [
    "input[data-cy='password-input-login']",
    "input[type='password']",
    "input[name='password']",
]
_SUBMIT_SELECTORS = [
    "button[data-cy='btn-submit']",
    "button[type='submit']",
    "button.q-btn[type='submit']",
]


def _first_visible(driver, selectors):
    for sel in selectors:
        for el in driver.find_elements(By.CSS_SELECTOR, sel):
            try:
                if el.is_displayed():
                    return el
            except Exception:  # noqa: BLE001 - stale element, keep looking
                continue
    return None


def _login_form(driver):
    """Return (username_el, password_el, submit_el_or_None) if a login form is
    visible on screen, else None."""
    pwd = _first_visible(driver, _PASS_SELECTORS)
    if pwd is None:
        return None
    user = _first_visible(driver, _USER_SELECTORS)
    submit = _first_visible(driver, _SUBMIT_SELECTORS)
    return user, pwd, submit


def _is_crash_page(driver):
    """True if the tab is showing Chrome's OWN crash/error page — e.g. the
    'Aw, Snap! Out of Memory' page — rather than the VHP app. A long-lived VHP
    window on a low-RAM PC can OOM-crash its renderer; Chrome then parks the tab
    on a chrome-error:// page until someone clicks Reload."""
    try:
        if (driver.current_url or "").startswith("chrome-error://"):
            return True
    except WebDriverException:
        return True  # can't even read the URL -> the tab is gone/crashed
    try:
        src = (driver.page_source or "")[:4000].lower()
    except WebDriverException:
        return True
    return ("aw, snap" in src or "err_out_of_memory" in src
            or "error code: out of memory" in src)


def _safe_get(driver, url, tries=2):
    """Navigate to url, auto-recovering from a crashed ('Aw, Snap!') tab.

    Normally a human has to click Reload after an OOM crash. This does it for
    us: if the navigation throws (renderer died mid-load) or lands on Chrome's
    own error page, we wait a moment and reload once more. By the second try the
    renderer has been re-spawned, so the VHP page loads and the scrape/keep-alive
    carries on instead of failing. If it's STILL crashed after the last try we
    raise, so the caller logs a real crash rather than mislabelling it a logout."""
    last_exc = None
    for attempt in range(1, tries + 1):
        try:
            driver.get(url)
        except WebDriverException as exc:  # renderer crashed mid-navigation
            last_exc = exc
            time.sleep(2)
            continue
        if _is_crash_page(driver):
            last_exc = None
            if attempt < tries:
                time.sleep(2)
                continue
            raise WebDriverException(
                "tab is on Chrome's crash page after {} load attempt(s) "
                "(likely 'Aw, Snap! Out of Memory') — the window needs a reload "
                "or the PC is low on memory".format(tries))
        return  # loaded the real page
    # Out of retries after navigation kept throwing — surface the last error.
    raise last_exc if last_exc is not None else WebDriverException(
        "could not navigate to {}".format(url))


# ---------------------------------------------------------------------------
# login-state reading (WITHOUT navigating) + opt-in auto-CAPTCHA login
# ---------------------------------------------------------------------------
def _read_login_state(driver, settle=2.0):
    """Read the attached window's login state WITHOUT navigating — so it can never
    disturb a login a human is typing. Returns one of:
        'expired' - VHP's 'session expired' dialog is up
        'login'   - a login form (password field) is visible
        'in'      - positively logged in (the report SEARCH button is present)
        'unknown' - none of the above yet (blank / mid-route / some other page)
    Polls briefly so a transient sample taken mid-SPA-route isn't misread (a window
    that is a split-second between 'dialog dismissed' and '/login rendered' would
    otherwise look logged-in)."""
    deadline = time.time() + settle
    while True:
        if _session_expired(driver):
            return "expired"
        if _login_form(driver) is not None:
            return "login"
        if driver.find_elements(By.XPATH, SEARCH_BTN):
            return "in"
        if time.time() >= deadline:
            return "unknown"
        time.sleep(0.4)


def _captcha_input(driver):
    """The 'Enter CAPTCHA' text box on the login form, or None."""
    return _first_visible(driver, [
        "input[placeholder*='captcha' i]",
        "input[name*='captcha' i]",
        "input[data-cy*='captcha' i]",
        "input[id*='captcha' i]",
    ])


def _on_login_url(driver):
    """True if the window's URL is VHP's /login route. Used so we never NAVIGATE a
    window that's really on the login page even when the form hasn't painted yet
    (an 'unknown' read) — reloading it would wipe a CAPTCHA a human may be typing."""
    try:
        return "/login" in (driver.current_url or "").lower()
    except WebDriverException:
        return False


def _clear_fields(els):
    """Best-effort clear of inputs the bot itself typed into (so a failed auto-login
    doesn't leave stale digits/creds for a human to submit by reflex). Never raises;
    the page may have navigated out from under the elements."""
    for el in els or []:
        try:
            el.clear()
        except Exception:  # noqa: BLE001 - stale / navigated away
            pass


# --- auto-CAPTCHA circuit breaker: per-property state in logs/.captcha_<code>.json
# This is the safety net that makes auto-login un-hammerable: one submit per
# detected logout, a cooldown between submits, and auto-OFF after N failures until
# a human logs in (which re-arms it). Failed logins are what lock the account, so
# the breaker caps them hard, and every operation fails SAFE (a state file that
# can't be read or written blocks auto-login rather than silently re-enabling it).
_CAP_FRESH = {"last_attempt": 0, "failures": 0, "disabled": False}
_CAP_UNREADABLE = "unreadable"  # file EXISTS but is corrupt/partial -> treat as blocked


def _cap_state_path(prop):
    return os.path.join(config.LOGS_DIR, ".captcha_{}.json".format(prop["code"]))


def _cap_state_read(prop):
    """Return the breaker state dict, or _CAP_UNREADABLE if the file EXISTS but can't
    be parsed. A MISSING file is a genuine first run -> fresh. This distinction is
    what makes the breaker fail SAFE: corruption blocks, it never re-enables."""
    path = _cap_state_path(prop)
    try:
        if not os.path.exists(path):
            return dict(_CAP_FRESH)
    except OSError:
        return _CAP_UNREADABLE
    try:
        with open(path) as fh:
            st = json.load(fh)
        return st if isinstance(st, dict) else _CAP_UNREADABLE
    except Exception:  # noqa: BLE001 - exists but corrupt/partial/locked
        return _CAP_UNREADABLE


def _cap_state_write(prop, st):
    """Write the breaker state ATOMICALLY (temp file + fsync + os.replace), so a
    crash / full disk can never leave a 0-byte or partial file. Returns True only if
    the write durably landed — a False tells the caller NOT to proceed with a login
    (an un-recordable attempt would be an un-counted, i.e. unbounded, one)."""
    path = _cap_state_path(prop)
    tmp = path + ".tmp"
    try:
        os.makedirs(config.LOGS_DIR, exist_ok=True)
        with open(tmp, "w") as fh:
            json.dump(st, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return True
    except Exception:  # noqa: BLE001 - disk full / locked / permission
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def _captcha_blocked(prop):
    """Return a human-readable reason auto-login must NOT run now, else None."""
    if not config.auto_captcha_enabled():
        return "auto-captcha is off (config flag / kill-file / env var)"
    st = _cap_state_read(prop)
    if st == _CAP_UNREADABLE:
        return ("breaker state file unreadable/corrupt — refusing to auto-login "
                "until a human logs in (which rewrites it) or the file is removed")
    if st.get("disabled"):
        return ("auto-captcha OFF after {} failed login(s) in a row — a human must "
                "log in once to re-arm it".format(st.get("failures", 0)))
    waited = time.time() - st.get("last_attempt", 0)
    if waited < config.AUTO_CAPTCHA_COOLDOWN_SEC:
        return "in cooldown ({:.0f}s to go)".format(
            config.AUTO_CAPTCHA_COOLDOWN_SEC - waited)
    return None


def _captcha_record_failure(prop):
    """Count ONE login attempt as a FAILURE (pessimistic) and set the cooldown; trip
    the breaker at AUTO_CAPTCHA_MAX_FAILURES. Returns (state, persisted). persisted
    is False if the write didn't durably land — the caller must then NOT submit, so
    an un-recordable attempt is never actually made."""
    st = _cap_state_read(prop)
    if st == _CAP_UNREADABLE or not isinstance(st, dict):
        st = dict(_CAP_FRESH)
    st["failures"] = int(st.get("failures", 0)) + 1
    st["last_attempt"] = int(time.time())
    if st["failures"] >= config.AUTO_CAPTCHA_MAX_FAILURES:
        st["disabled"] = True
    persisted = _cap_state_write(prop, st)
    return st, persisted


def _captcha_reset(prop):
    """Re-arm auto-login after we OBSERVE a logged-in window: clear the failure
    count, the cooldown and the auto-off flag (and overwrite a corrupt file). Only
    writes when there's something to clear or fix."""
    st = _cap_state_read(prop)
    if (st == _CAP_UNREADABLE or st.get("failures") or st.get("disabled")
            or st.get("last_attempt")):
        _cap_state_write(prop, dict(_CAP_FRESH))


def attempt_auto_login(driver, prop, recovery, logger=None):
    """Best-effort: when the BOT'S OWN recovery brought the window to VHP's login
    page, OCR the CAPTCHA and log in. Returns True ONLY if a positive logged-in page
    is confirmed. Never raises.

    `recovery` must be True — it means the bot itself reached this login page THIS
    tick (it dismissed the expired dialog, or its own navigation bounced to /login).
    We refuse to auto-submit on a login page we merely FOUND already open, because
    that's exactly where a human doing a manual login would be.

    Un-hammerable by design:
      * master switch + kill-file/env; per-property cooldown + circuit-breaker
        (auto-OFF after N fails until a human logs in); exactly ONE submit per call;
      * backs off if a human is typing (CAPTCHA box non-empty) — re-checked AFTER the
        OCR, right before submitting, to close the typing-during-OCR race;
      * the attempt is counted BEFORE the submit and only cleared on a CONFIRMED
        positive login, so a crash/transient can neither drop nor fake it, and a
        state file that can't be persisted aborts the submit entirely;
      * only submits a CAPTCHA the OCR read CONFIDENTLY; needs Tesseract installed.
    """
    import captcha  # local import: OCR deps are optional, keep them off the hot path

    def _log(msg):
        if logger is not None:
            logger.info("[%s] auto-login: %s", prop["code"], msg)

    if not recovery:
        return False  # never auto-submit on a login page we didn't just create
    reason = _captcha_blocked(prop)
    if reason:
        _log(reason)
        return False
    if not captcha.ocr_available():
        _log("OCR not installed ({}) — run `pip install pytesseract pillow` + the "
             "Tesseract engine to enable it".format(captcha.ocr_unavailable_reason()))
        return False

    # Wait briefly for the login form + CAPTCHA box (after an OK click the redirect
    # to /login takes a moment).
    form = cap_in = None
    end = time.time() + 8
    while time.time() < end:
        try:
            form = _login_form(driver)
            cap_in = _captcha_input(driver)
        except WebDriverException:
            return False
        if form is not None and cap_in is not None:
            break
        time.sleep(0.4)
    if form is None or cap_in is None:
        _log("login form / CAPTCHA box not found — nothing to do")
        return False

    def _box_has_text():
        try:
            return bool((cap_in.get_attribute("value") or "").strip())
        except Exception:  # noqa: BLE001
            return False

    # Never fight a human already typing the CAPTCHA.
    if _box_has_text():
        _log("CAPTCHA box already has text — a human is logging in; backing off")
        return False

    # Fill username/password only if empty (the browser usually remembers them);
    # track what WE filled so we can clear it again on failure.
    user_el, pwd_el, submit_el = form
    creds = config.get_credentials(prop["code"])
    filled = []
    try:
        if user_el is not None and not (user_el.get_attribute("value") or "").strip() and creds:
            user_el.clear()
            user_el.send_keys(creds[0])
            filled.append(user_el)
        if pwd_el is not None and not (pwd_el.get_attribute("value") or "").strip() and creds:
            pwd_el.clear()
            pwd_el.send_keys(creds[1])
            filled.append(pwd_el)
    except Exception:  # noqa: BLE001 - keep going; we re-check below
        pass
    try:
        have_user = bool((user_el.get_attribute("value") or "").strip()) if user_el else False
        have_pwd = bool((pwd_el.get_attribute("value") or "").strip()) if pwd_el else False
    except Exception:  # noqa: BLE001
        have_user = have_pwd = False
    if not have_user or not have_pwd:
        _log("no username/password available (browser didn't remember them and no "
             "credentials.py) — handing to a human")
        _clear_fields(filled)
        return False

    digits = captcha.solve_captcha(driver, config.CAPTCHA_MIN_LEN, config.CAPTCHA_MAX_LEN)
    if not digits:
        _log("CAPTCHA not read confidently — handing to a human (NO attempt made)")
        _clear_fields(filled)
        return False

    # TOCTOU guard: a human may have started typing DURING the (multi-second) OCR.
    # Re-check right before we touch the box, and abort BEFORE counting an attempt.
    if _box_has_text():
        _log("a human started typing the CAPTCHA during OCR — backing off (no submit)")
        _clear_fields(filled)
        return False

    # Count the attempt as a FAILURE up front (sets cooldown + advances the breaker).
    # Any exit after this — success, timeout, or a renderer crash — leaves the
    # breaker correctly advanced; we only CLEAR it on a confirmed positive login. If
    # the state can't be persisted, DON'T submit (an un-counted attempt is unbounded).
    st, persisted = _captcha_record_failure(prop)
    if not persisted:
        _log("could not save breaker state (disk full / logs locked?) — NOT "
             "submitting, to stay un-hammerable")
        _clear_fields(filled)
        return False

    try:
        cap_in.clear()
        cap_in.send_keys(digits)
        if submit_el is not None:
            _safe_click(driver, submit_el)
        else:
            cap_in.send_keys(Keys.RETURN)
    except Exception as exc:  # noqa: BLE001 - submit machinery failed; failure stands
        _log("could not submit the login: {}".format(_describe(exc)))
        return False

    # Success requires a POSITIVE, stable logged-in signal (the report SEARCH
    # button) — NOT merely 'the login form vanished', which flickers during a
    # re-render after a wrong submit and would wrongly reset the breaker.
    end = time.time() + 25
    while time.time() < end:
        try:
            if _read_login_state(driver, settle=1.0) == "in":
                _captcha_reset(prop)  # confirmed in -> clear the recorded failure
                _log("logged in OK (CAPTCHA '{}' accepted)".format(digits))
                return True
        except WebDriverException:
            break  # renderer died; the failure we recorded up front stands
        time.sleep(0.5)

    # Not confirmed logged in -> the up-front failure stands. Clear our wrong entry
    # (and any creds we filled) so a later human doesn't submit stale values.
    _clear_fields(filled + [cap_in])
    if st.get("disabled"):
        _log("login FAILED (CAPTCHA '{}') — {} fails in a row, auto-login now OFF "
             "until a human logs in".format(digits, st.get("failures")))
    else:
        _log("login FAILED (CAPTCHA '{}' likely misread) — handing to a human "
             "(fail {}/{})".format(digits, st.get("failures"),
                                   config.AUTO_CAPTCHA_MAX_FAILURES))
    return False


def _logged_out_msg(prop, closed):
    """Guidance text that matches what's actually on screen (P5): if the expired
    dialog was closed, the window is on the login page; if not, OK must be clicked
    first."""
    code = prop["code"]
    if closed:
        return ("{code}: VHP is logged out — the window is on the login page; type "
                "the CAPTCHA and click LOG IN in the '{code}' Chrome window, then "
                "re-run.".format(code=code))
    return ("{code}: VHP session expired — click OK in the '{code}' Chrome window, "
            "then type the CAPTCHA and LOG IN, then re-run.".format(code=code))


def _finish_login_state(driver, prop, state, login_is_recovery):
    """Handle a non-'in' login state: dismiss any expired dialog, and (only when the
    bot itself caused this login page this tick) try auto-login. Returns True if we
    end up logged in, else raises LoginRequired. `login_is_recovery` says whether a
    plain 'login' state was caused by the bot (True) or merely found already open
    (False -> never auto-submit; hand to a human)."""
    if state == "expired":
        closed = dismiss_session_expired(driver)  # click OK -> lands on login page
        if closed and attempt_auto_login(driver, prop, recovery=True):
            return True
        raise LoginRequired(_logged_out_msg(prop, closed))
    if state == "login":
        if login_is_recovery and attempt_auto_login(driver, prop, recovery=True):
            return True
        raise LoginRequired(_logged_out_msg(prop, True))
    raise LoginRequired(
        "{code}: could not confirm login (the report didn't render). Check the "
        "'{code}' Chrome window.".format(code=prop["code"]))


def ensure_logged_in(driver, prop, creds, probe_url):
    """Ensure the attached window is logged in, WITHOUT disturbing a login a human
    may be typing.

    We LOOK at the window as-is first and only navigate when it is NOT already on a
    login page — otherwise a scheduled scrape firing while someone hand-types the
    CAPTCHA would reload /login and wipe it (the user's #1 concern).

      * already logged in             -> return (open_report navigates to the report)
      * expired dialog up             -> click OK + (opt-in) auto-login; else raise
      * on /login already (bot didn't create it) -> hand to a human (never auto-
                                        submit where a human might be mid-login)
      * blank / other page            -> navigate to probe_url, then re-evaluate; a
                                        login page the BOT's navigation reveals is
                                        bot-initiated, so auto-login applies there
    (`creds` is accepted for backward-compatibility; credentials now come from
    config.get_credentials inside attempt_auto_login.)
    """
    state = _read_login_state(driver)
    if state == "in":
        _captcha_reset(prop)  # observed logged-in -> re-arm auto-login for next time
        return
    if state in ("expired", "login"):
        # A login page we FOUND already open is not a recovery we initiated.
        if _finish_login_state(driver, prop, state, login_is_recovery=False):
            _captcha_reset(prop)
            return

    # state == 'unknown'. If we're actually on /login (form just not painted yet),
    # treat it as a login page: do NOT navigate (that could wipe a human's entry).
    if _on_login_url(driver):
        raise LoginRequired(_logged_out_msg(prop, True))

    _safe_get(driver, probe_url)  # not a login page -> safe; also recovers a crash
    time.sleep(3)  # let the SPA render (app or login form)

    state = _read_login_state(driver)
    if state == "in":
        _captcha_reset(prop)
        return
    # A login/expired page our OWN navigation just revealed IS a bot-initiated
    # recovery, so auto-login is allowed here.
    if _finish_login_state(driver, prop, state, login_is_recovery=True):
        _captcha_reset(prop)
        return
    raise LoginRequired(
        "{code}: could not confirm login (the report didn't render). Check the "
        "'{code}' Chrome window.".format(code=prop["code"]))


def refresh_page(driver, settle=3.0):
    """Hard-reload the current page to keep the VHP session alive.

    A plain reload/navigation can be served from the browser cache WITHOUT really
    contacting VHP, so it may not reset VHP's inactivity timer. This does a
    cache-BYPASSING reload (CDP Page.reload with ignoreCache) so the browser truly
    re-contacts the server and the SPA re-authenticates with the saved cookie — the
    authenticated round-trip that actually keeps the session logged in. It also
    flushes the page's accumulated memory, which helps avoid the 'Aw, Snap! Out of
    Memory' crash. Crash-safe and never raises (keep-alive must stay best-effort)."""
    try:
        driver.execute_cdp_cmd("Page.reload", {"ignoreCache": True})
    except Exception:  # noqa: BLE001 - CDP cmd unavailable -> ordinary reload
        try:
            driver.refresh()
        except WebDriverException:
            return
    time.sleep(settle)  # let the reload fetch + the SPA re-bootstrap
    try:
        if _is_crash_page(driver):  # the reload itself OOM-crashed the tab -> recover
            driver.refresh()
            time.sleep(settle)
    except WebDriverException:
        pass


# ---------------------------------------------------------------------------
# per-report steps
# ---------------------------------------------------------------------------
def open_report(driver, report, require_search=True):
    """Deep-link to the report and wait for it to render.

    Detects login state at the REAL report page (not the /login route, which
    always shows a form): if the SEARCH button appears we're in; if a login
    field appears we were bounced to login -> raise LoginRequired.

    require_search=False is for OPERATIONAL pages that have no SEARCH button
    (e.g. the reservation grid /fr/reservation, room-departure
    /to/telephone-operator): navigate, let the SPA hydrate, confirm we weren't
    bounced to login, then return — instead of timing out waiting for a button
    that never appears. Used by the inspector and the JSON-page scrape flow.
    """
    # Look first — if the window is already on a login page, DON'T navigate (that
    # would reload /login and wipe a CAPTCHA a human may be typing). Normally we get
    # here already logged in (ensure_logged_in ran first), so this just navigates.
    pre = _read_login_state(driver)
    if pre in ("expired", "login") or (pre == "unknown" and _on_login_url(driver)):
        if pre == "expired":
            dismiss_session_expired(driver)
        raise LoginRequired(
            "VHP is logged out on report {!r} — the window is on the login page; "
            "type the CAPTCHA and click LOG IN, then re-run.".format(report["name"]))

    _safe_get(driver, report["url"])  # recovers a crashed ('Aw, Snap!') tab

    if not require_search:
        # No SEARCH button to wait on. Give the SPA time to hydrate, then make sure
        # we weren't bounced to login before handing the page to the caller.
        time.sleep(4.0)
        if _session_expired(driver):
            dismiss_session_expired(driver)
            raise LoginRequired(
                "VHP session expired on {!r} — the window is now on the login page; "
                "type the CAPTCHA and click LOG IN, then re-run.".format(report["name"]))
        for sel in _LOGIN_FIELDS:
            if any(e.is_displayed() for e in driver.find_elements(By.CSS_SELECTOR, sel)):
                raise LoginRequired(
                    "bounced to login on {!r}".format(report["name"]))
        return

    end = time.time() + 40
    while time.time() < end:
        # Check the 'session expired' dialog FIRST — a stale report may still be
        # behind it, so the SEARCH button could otherwise look like "logged in".
        if _session_expired(driver):
            dismiss_session_expired(driver)  # click OK -> window lands on the login page
            raise LoginRequired(
                "VHP session expired on report {!r} — the window is now on the login "
                "page; just type the CAPTCHA and click LOG IN in that Chrome window, "
                "then re-run.".format(report["name"]))
        if driver.find_elements(By.XPATH, SEARCH_BTN):
            return  # report rendered -> logged in
        for sel in _LOGIN_FIELDS:
            if any(e.is_displayed() for e in driver.find_elements(By.CSS_SELECTOR, sel)):
                raise LoginRequired(
                    "bounced to login on report {!r}".format(report["name"])
                )
        time.sleep(0.5)
    raise TimeoutError("report page did not render: {}".format(report["url"]))


# VHP renders the Date filter as DD/MM/YY (e.g. 12/10/26 = 12 Oct 2026).
_VHP_DATE_FMT = "%d/%m/%y"

# JS that sets an <input>'s value the way Vue/Quasar notices (a raw el.value =
# ... is ignored by the component's v-model). We call the native setter, then
# fire the events Quasar listens on.
_JS_SET_INPUT = (
    "var el=arguments[0], v=arguments[1];"
    "var d=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value');"
    "if(d&&d.set){d.set.call(el,v);}else{el.value=v;}"
    "el.dispatchEvent(new Event('input',{bubbles:true}));"
    "el.dispatchEvent(new Event('change',{bubbles:true}));"
    "el.dispatchEvent(new Event('blur',{bubbles:true}));"
)


def _parse_iso(s):
    """Parse an ISO 'YYYY-MM-DD' string to a date (for on-demand explicit ranges).
    Passes a date straight through."""
    if isinstance(s, datetime.date):
        return s
    return datetime.datetime.strptime(str(s).strip(), "%Y-%m-%d").date()


def _period_range(report):
    """Return (start_date, end_date) datetime.date for report['period'], or None
    to leave VHP's default untouched.

        {'start': 'YYYY-MM-DD', 'end': 'YYYY-MM-DD'}  -> an EXACT range (used by
                         on-demand jobs; the end is inclusive, exactly as typed)
        'last_7_days'   -> the 7 days ending today        (today-6 .. today)
        'current_month' -> 1st of this month .. today
        {'days_back': N, 'include_today': bool}  -> custom trailing window
        None / anything else -> None
    """
    today = datetime.date.today()
    p = report.get("period")
    if isinstance(p, dict):
        # Explicit range wins — an on-demand job set exact dates; honour them as-is.
        if p.get("start") and p.get("end"):
            return _parse_iso(p["start"]), _parse_iso(p["end"])
        n = max(1, int(p.get("days_back", 7)))
        end = today if p.get("include_today", True) else today - datetime.timedelta(days=1)
        return end - datetime.timedelta(days=n - 1), end
    if p == "last_7_days":
        return today - datetime.timedelta(days=6), today
    if p == "current_month":
        return today.replace(day=1), today
    return None


def _el_html(driver, el, limit=1600):
    """Trimmed, whitespace-collapsed outerHTML of an element (for diagnostics)."""
    try:
        html = driver.execute_script("return arguments[0].outerHTML;", el) or ""
    except Exception:  # noqa: BLE001
        return "(outerHTML unavailable)"
    html = " ".join(html.split())
    return html + "" if len(html) <= limit else html[:limit] + " …(truncated)"


_QFIELD_ANCESTOR = (
    "ancestor-or-self::*"
    "[contains(concat(' ',normalize-space(@class),' '),' q-field ')][1]"
)


def _field_container(driver, el):
    """Return a container to operate on: the nearest .q-field wrapping `el`,
    else `el`'s parent, else `el` itself. (Operating on a bare <input> breaks
    read-back, since an input has no descendants to re-read.)"""
    try:
        qf = el.find_elements(By.XPATH, _QFIELD_ANCESTOR)
        if qf:
            return qf[0]
    except Exception:  # noqa: BLE001
        pass
    try:
        return el.find_element(By.XPATH, "..")
    except Exception:  # noqa: BLE001
        return el


def _find_date_field(driver):
    """Locate the Date filter control, robust to VHP not using a Quasar floating
    label (the 'Date' text sits ABOVE the box and the range lives in an input's
    VALUE, not as element text). Tried in order:
      1. any visible input whose VALUE looks like a date / date-range
      2. a q-field with a floating 'Date' label
      3. an external 'Date' label, then the next field control after it
      4. any q-field whose text OR a descendant input value looks like a range
    """
    # 1) the input that actually holds the date value (most reliable here).
    for inp in driver.find_elements(By.CSS_SELECTOR, "input"):
        try:
            if not inp.is_displayed():
                continue
            v = inp.get_attribute("value") or ""
            if _looks_like_range(v) or _parse_dates(v):
                return _field_container(driver, inp)
        except Exception:  # noqa: BLE001
            continue

    # 2) a q-field whose floating label is 'Date' (strip a required '*').
    by_label = (
        "//*[contains(concat(' ',normalize-space(@class),' '),' q-field ')]"
        "[.//*[contains(@class,'q-field__label')]"
        "[translate(normalize-space(translate(.,'*','')),'{lo}','{up}')='DATE']]"
    ).format(lo=_LOWER, up=_UPPER)
    for el in driver.find_elements(By.XPATH, by_label):
        if el.is_displayed():
            return el

    # 3) an EXTERNAL label/text equal to 'Date' -> the next q-field after it.
    lbl_xp = (
        "//*[translate(normalize-space(translate(.,'*','')),'{lo}','{up}')='DATE']"
        "[not(.//*[normalize-space(.)!=''])]"
    ).format(lo=_LOWER, up=_UPPER)
    for lbl in driver.find_elements(By.XPATH, lbl_xp):
        try:
            if not lbl.is_displayed():
                continue
            ctrl = lbl.find_element(
                By.XPATH,
                "following::*[contains(concat(' ',normalize-space(@class),' '),' q-field ')][1]",
            )
            if ctrl.is_displayed():
                return ctrl
        except Exception:  # noqa: BLE001
            continue

    # 4) any q-field whose text or a descendant input value looks like a range.
    for el in driver.find_elements(By.CSS_SELECTOR, ".q-field"):
        try:
            if not el.is_displayed():
                continue
            if _looks_like_range((el.text or "").replace("\n", " ")):
                return el
            for inp in el.find_elements(By.CSS_SELECTOR, "input"):
                if _looks_like_range(inp.get_attribute("value") or ""):
                    return el
        except Exception:  # noqa: BLE001
            continue
    return None


def _dump_date_diag(driver):
    """JS dump of every input (value/readonly/placeholder/class) and the first
    element whose text looks like a date, with its parent — so the Date control
    is fully identifiable from one run even when the q-field heuristics miss."""
    js = r"""
    var out = [];
    var ins = document.querySelectorAll('input');
    out.push('INPUTS(' + ins.length + '):');
    for (var i=0;i<ins.length;i++){
      var e=ins[i];
      out.push('  <input type=' + (e.type||'') + ' ro=' + e.readOnly +
               ' val=' + JSON.stringify((e.value||'').slice(0,32)) +
               ' ph=' + JSON.stringify((e.placeholder||'').slice(0,24)) +
               ' class=' + JSON.stringify((e.className||'').slice(0,70)) + '>');
    }
    var re=/\d{1,2}\/\d{1,2}\/\d{2,4}/;
    var all=document.querySelectorAll('body *');
    for (var j=0;j<all.length;j++){
      var el=all[j];
      if (el.children.length>2) continue;
      var t=(el.textContent||'').trim();
      if (re.test(t) && t.length<48){
        out.push('DATE-TEXT <' + el.tagName + ' class=' +
                 JSON.stringify((el.className||'').slice(0,70)) + '> ' + JSON.stringify(t));
        var p=el.parentElement;
        if(p){ out.push('  parent <' + p.tagName + ' class=' +
                 JSON.stringify((p.className||'').slice(0,90)) + '>'); }
        break;
      }
    }
    return out.join('\n');
    """
    try:
        return driver.execute_script(js)
    except Exception as exc:  # noqa: BLE001
        return "(diag failed: {})".format(_describe(exc))


def dump_tables(driver, max_rows=40, max_tables=6):
    """Diagnostic: dump every visible data table/grid on the page as TSV (header +
    up to `max_rows` rows each), so we can SEE the columns + sample data of a page
    we haven't wired yet (e.g. the reservation grid's agent/room-nights columns, or
    the room-departure list). Handles plain <table> and Quasar <div class="q-table">
    grids (which use role=grid / .q-td rather than <td>). Never raises."""
    js = r"""
    var MAXR = arguments[0], MAXT = arguments[1];
    function vis(e){ var r=e.getBoundingClientRect(); return r.width>1 && r.height>1; }
    function cellText(c){ return (c.innerText||c.textContent||'').trim().replace(/\s+/g,' '); }
    var out = [];
    // 1) real <table>s
    var tables = Array.prototype.slice.call(document.querySelectorAll('table')).filter(vis);
    // 2) Quasar grids that aren't a <table> (role=grid / .q-table wrapper)
    var grids = Array.prototype.slice.call(
        document.querySelectorAll('[role=grid], .q-table__container')).filter(vis);
    var all = tables.concat(grids).slice(0, MAXT);
    if (!all.length) { return '(no visible tables/grids found)'; }
    all.forEach(function(t, ti){
      out.push('===== TABLE #' + (ti+1) + ' =====');
      // header: <th>, or role=columnheader, or the first row's cells
      var heads = Array.prototype.slice.call(
          t.querySelectorAll('th, [role=columnheader]')).filter(vis).map(cellText);
      if (heads.length) out.push('HEAD\t' + heads.join('\t'));
      // rows: <tr> with <td>, or role=row with role=gridcell/.q-td
      var rows = Array.prototype.slice.call(t.querySelectorAll('tr, [role=row]'));
      var printed = 0, total = 0;
      for (var i=0; i<rows.length; i++){
        var r = rows[i];
        var cells = Array.prototype.slice.call(
            r.querySelectorAll('td, [role=gridcell], .q-td'));
        if (!cells.length) continue;          // header row / spacer
        total++;
        if (printed < MAXR){
          out.push(cells.map(cellText).join('\t'));
          printed++;
        }
      }
      out.push('(' + total + ' data row(s); showed ' + printed + ')');
    });
    return out.join('\n');
    """
    try:
        return driver.execute_script(js, max_rows, max_tables)
    except Exception as exc:  # noqa: BLE001
        return "(table dump failed: {})".format(_describe(exc))


def _looks_like_range(txt):
    """True if txt contains something like '12/10/26 - 12/10/26'."""
    return bool(re.search(r"\d{1,2}/\d{1,2}/\d{2,4}\s*-\s*\d{1,2}/\d{1,2}/\d{2,4}", txt or ""))


def _parse_dates(txt):
    """Every dd/mm/yy(yy) date in txt as a (day, month, 2-digit-year) tuple."""
    out = []
    for d, m, y in re.findall(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", txt or ""):
        out.append((int(d), int(m), int(y) % 100))
    return out


def _date_field_value(driver, field):
    """The Date field's currently-shown value, e.g. '12/10/26 - 12/10/26'.
    Aggregates ALL input values first (a two-input from/to field would otherwise
    expose only the start date, so the end could never verify), then falls back
    to the control's display text. Fully guarded — a stale field must never raise
    (set_period is non-fatal)."""
    try:
        vals = []
        for el in field.find_elements(By.CSS_SELECTOR, "input"):
            try:
                v = (el.get_attribute("value") or "").strip()
                if v:
                    vals.append(v)
            except Exception:  # noqa: BLE001
                continue
        if vals:
            return " ".join(vals)
        for css in (".q-field__native", ".ellipsis"):
            for el in field.find_elements(By.CSS_SELECTOR, css):
                try:
                    txt = (el.text or el.get_attribute("textContent") or "").strip()
                    if txt:
                        return txt
                except Exception:  # noqa: BLE001
                    continue
        return (field.text or "").replace("\n", " ").strip()
    except Exception:  # noqa: BLE001
        return ""


def _range_matches(shown, start, end):
    """True if the field's shown value reflects start..end. Compares the parsed
    day/month/year NUMBERS, so it's tolerant of spacing, separator and whether
    VHP zero-pads ('1/2/26' matches '01/02/26')."""
    found = set(_parse_dates(shown))
    want_s = (start.day, start.month, start.year % 100)
    want_e = (end.day, end.month, end.year % 100)
    return want_s in found and want_e in found


def _visible_inputs(field):
    out = []
    try:
        for i in field.find_elements(By.CSS_SELECTOR, "input"):
            try:
                if i.is_displayed():
                    out.append(i)
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return out


def _editable_inputs(field):
    """Visible inputs that are NOT readonly/disabled (so typing can work). A
    Quasar range field is usually readonly -> this is empty -> use the calendar."""
    out = []
    for i in _visible_inputs(field):
        try:
            if not i.get_attribute("readonly") and not i.get_attribute("disabled"):
                out.append(i)
        except Exception:  # noqa: BLE001
            continue
    return out


def _wait_css(driver, css, timeout):
    """True once any element matching css is visible, within timeout seconds."""
    end = time.time() + timeout
    while time.time() < end:
        for el in driver.find_elements(By.CSS_SELECTOR, css):
            try:
                if el.is_displayed():
                    return True
            except Exception:  # noqa: BLE001
                continue
        time.sleep(0.15)
    return False


def _close_overlays(driver):
    """Dismiss any open Quasar popup/calendar so a leftover overlay can't block
    configure_report or SEARCH. ESC twice (popup + its proxy)."""
    for _ in range(2):
        try:
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.25)


# A BACKDROP is the only reliable signal of a *blocking* overlay (a modal dialog
# or an overlay drawer like VHP's Notification panel dims the page behind one).
# A standard always-present drawer — e.g. VHP's 50px right icon bar
# (q-drawer--standard) — has NO backdrop and must be left alone.
_OVERLAY_BACKDROPS = ".q-dialog__backdrop, .q-drawer__backdrop"


def dismiss_blocking_dialog(driver):
    """Close a Quasar overlay (modal dialog or overlay drawer, e.g. VHP's
    Notification panel) ONLY when its backdrop is dimming the page and
    intercepting clicks. Keyed strictly on a visible backdrop, so permanent
    layout drawers are never touched. Closes gently without clicking inside any
    panel: ESC, a backdrop click, then neutralise any backdrop still left."""
    backdrops = [b for b in driver.find_elements(By.CSS_SELECTOR, _OVERLAY_BACKDROPS)
                 if _disp_safe(b)]
    if not backdrops:
        return False
    try:
        html = driver.execute_script(
            "var d=document.querySelector('.q-dialog__inner, .q-menu, "
            ".q-drawer--mobile, .q-drawer--right.q-drawer--standard + * .q-drawer');"
            "return d ? d.outerHTML.replace(/\\s+/g,' ').slice(0,350) : '(backdrop only)';")
        print("    (overlay) blocking backdrop present -> {}".format(html))
    except Exception:  # noqa: BLE001
        pass
    _close_overlays(driver)                       # ESC
    for b in backdrops:                           # click the dimmed area to close it
        try:
            _force_click(driver, b)
            time.sleep(0.3)
        except Exception:  # noqa: BLE001
            continue
    for el in driver.find_elements(By.CSS_SELECTOR, "[data-cy*='close']"):  # a safe ✕
        try:
            if el.is_displayed():
                _force_click(driver, el)
                time.sleep(0.2)
        except Exception:  # noqa: BLE001
            continue
    if [b for b in driver.find_elements(By.CSS_SELECTOR, _OVERLAY_BACKDROPS) if _disp_safe(b)]:
        try:
            driver.execute_script(
                "document.querySelectorAll(arguments[0]).forEach("
                "function(b){b.style.pointerEvents='none';b.style.display='none';});",
                _OVERLAY_BACKDROPS)
            print("    (overlay) backdrop left open — neutralised so clicks pass through")
        except Exception:  # noqa: BLE001
            pass
    return True


def _disp_safe(el):
    try:
        return el.is_displayed()
    except Exception:  # noqa: BLE001
        return False


def _try_type_range(driver, field, start, end):
    """Type the range into an EDITABLE input (skips readonly calendar-only
    fields). Commits with TAB, never ENTER (ENTER can submit SEARCH early)."""
    s = start.strftime(_VHP_DATE_FMT)
    e = end.strftime(_VHP_DATE_FMT)
    inputs = _editable_inputs(field)
    if not inputs:
        return False

    def _fill(inp, text):
        try:
            inp.click()
        except Exception:  # noqa: BLE001
            pass
        try:
            inp.send_keys(Keys.CONTROL, "a")
            inp.send_keys(Keys.DELETE)
        except Exception:  # noqa: BLE001
            pass
        inp.send_keys(text)

    try:
        if len(inputs) >= 2:
            _fill(inputs[0], s)
            _fill(inputs[1], e)
        else:
            _fill(inputs[0], "{} - {}".format(s, e))
        inputs[-1].send_keys(Keys.TAB)
    except Exception:  # noqa: BLE001
        return False
    time.sleep(0.6)
    return True


# --- QDate calendar-popup driver (the reliable path for a readonly range) ----
_MARK_ALL_JS = (
    "document.querySelectorAll('*').forEach("
    "function(e){e.setAttribute('data-vhp-seen','1');});"
)

# Returns the ROOT elements of any newly-added, reasonably-sized visible subtree
# (a popup/calendar portals in as new DOM). Skips tiny ripples/tooltips.
_NEW_ROOTS_JS = r"""
var roots=[], all=document.querySelectorAll('*');
for (var i=0;i<all.length;i++){
  var e=all[i];
  if(e.hasAttribute('data-vhp-seen')) continue;
  var p=e.parentElement;
  if(p && !p.hasAttribute('data-vhp-seen')) continue;  // only the top of a new subtree
  var r=e.getBoundingClientRect();
  if(r.width<40 || r.height<40) continue;              // skip ripples/tooltips
  roots.push(e);
}
return roots;
"""

# Fire the events a custom picker might open on (not just a plain click).
_FIRE_OPEN_JS = (
    "var e=arguments[0];"
    "try{e.focus();}catch(_){}"
    "['pointerdown','mousedown','mouseup','click'].forEach(function(t){"
    "try{e.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window}));}"
    "catch(_){}});"
)


def _largest(els):
    """The biggest element by area (the popup, vs incidental new nodes)."""
    best, area = None, -1
    for e in els:
        try:
            s = e.size
            a = s["width"] * s["height"]
            if a > area:
                best, area = e, a
        except Exception:  # noqa: BLE001
            continue
    return best or (els[0] if els else None)


def _new_visible_roots(driver):
    try:
        return driver.execute_script(_NEW_ROOTS_JS) or []
    except Exception:  # noqa: BLE001
        return []


def _open_and_capture_popup(driver, field):
    """Open the date popup by a DOM diff: mark the whole page, then click (and,
    if needed, fire pointer/mouse/focus events on) the calendar icon / control /
    input / field, and capture whatever NEW element appears — robust to the
    popup's class names. Returns (popup_element_or_None, dump_string)."""
    try:
        driver.execute_script(_MARK_ALL_JS)
    except Exception:  # noqa: BLE001
        pass
    openers = []
    for css in (".q-field__append .q-icon", "i.mdi-calendar", ".q-field__append",
                ".q-field__control", ".q-field__native", "input"):
        try:
            openers.extend(field.find_elements(By.CSS_SELECTOR, css))
        except Exception:  # noqa: BLE001
            continue
    openers.append(field)
    for op in openers:
        try:
            if not op.is_displayed():
                continue
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", op)
        except Exception:  # noqa: BLE001
            continue
        _safe_click(driver, op)
        time.sleep(1.0)
        roots = _new_visible_roots(driver)
        if not roots:  # retry with explicit pointer/mouse/focus events
            try:
                driver.execute_script(_FIRE_OPEN_JS, op)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.0)
            roots = _new_visible_roots(driver)
        if roots:
            dump = "\n   --- ".join(_el_html(driver, r, 1700) for r in roots[:4])
            return _largest(roots), dump
    return None, "(nothing new appeared after clicking the icon/control/input/field)"


def _qdate_nav_text_buttons(driver):
    """The text buttons in the QDate navigation bar, in order [month, year]
    (chevrons carry an icon and no text, so they're filtered out)."""
    seen, out = set(), []
    for b in driver.find_elements(
            By.CSS_SELECTOR, ".q-date__navigation .q-btn, .q-date__navigation button"):
        try:
            if b.is_displayed() and (b.text or "").strip() and b.id not in seen:
                seen.add(b.id)
                out.append(b)
        except Exception:  # noqa: BLE001
            continue
    return out


def _click_text_item(driver, css, text):
    """Click a view item (year/month cell) whose text equals `text`, scrolling
    the scrollable view a few times if it's not rendered yet."""
    for _ in range(14):
        for el in driver.find_elements(By.CSS_SELECTOR, css):
            try:
                if el.is_displayed() and (el.text or "").strip() == text:
                    _safe_click(driver, el)
                    return True
            except Exception:  # noqa: BLE001
                continue
        driver.execute_script(
            "var m=document.querySelector('.q-date__years, .q-date__months, .q-date__view');"
            "if(m){m.scrollTop=m.scrollTop+160;}")
        time.sleep(0.15)
    return False


def _qdate_goto(driver, year, month):
    """Navigate the open QDate to (year, month) using the year/month picker views
    — locale-independent: year chosen by its number, month by its position."""
    navs = _qdate_nav_text_buttons(driver)
    opened = False
    if len(navs) >= 2:
        _safe_click(driver, navs[1])  # the year label
        opened = _wait_css(driver, ".q-date__years", 1.5)
    if not opened:  # fallback: the header year subtitle also opens the years view
        for sub in driver.find_elements(By.CSS_SELECTOR, ".q-date__header-subtitle"):
            try:
                if sub.is_displayed():
                    _safe_click(driver, sub)
                    opened = _wait_css(driver, ".q-date__years", 1.5)
                    break
            except Exception:  # noqa: BLE001
                continue
    if opened:
        _click_text_item(driver, ".q-date__years-item .q-btn, .q-date__years-item, "
                                  ".q-date__years button", str(year))
        time.sleep(0.4)

    navs = _qdate_nav_text_buttons(driver)
    if navs:
        _safe_click(driver, navs[0])  # the month label
        if _wait_css(driver, ".q-date__months", 1.5):
            items = []
            for m in driver.find_elements(
                    By.CSS_SELECTOR, ".q-date__months-item, .q-date__months button"):
                try:
                    if m.is_displayed():
                        items.append(m)
                except Exception:  # noqa: BLE001
                    continue
            if 1 <= month <= len(items):
                _safe_click(driver, items[month - 1])
                time.sleep(0.4)


def _click_qday(driver, day):
    """Click the IN-month day cell with the given number (never an --out
    spillover cell from the adjacent month, which shares day numbers)."""
    want = str(int(day))
    for css in (".q-date__calendar-item--in button", ".q-date__calendar-item--in"):
        for el in driver.find_elements(By.CSS_SELECTOR, css):
            try:
                if el.is_displayed() and (el.text or "").strip() == want:
                    _safe_click(driver, el)
                    return True
            except Exception:  # noqa: BLE001
                continue
    return False


def _vc_rendered_dates(driver):
    """Every date currently rendered in the V-Calendar (from each cell's
    'id-YYYY-MM-DD' class), to decide which way to page."""
    out = set()
    for el in driver.find_elements(By.CSS_SELECTOR, ".vc-day"):
        try:
            m = re.search(r"\bid-(\d{4})-(\d{2})-(\d{2})\b", el.get_attribute("class") or "")
            if m:
                out.add(datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except Exception:  # noqa: BLE001
            continue
    return out


def _vc_click_arrow(driver, direction):
    """Click the V-Calendar prev/next month arrow (< or >)."""
    sels = {
        "left": [".vc-arrow.is-left", ".vc-prev", ".vc-arrow.vc-prev",
                 "[class*='vc-arrow'][class*='left']"],
        "right": [".vc-arrow.is-right", ".vc-next", ".vc-arrow.vc-next",
                  "[class*='vc-arrow'][class*='right']"],
    }[direction]
    for sel in sels:
        for el in driver.find_elements(By.CSS_SELECTOR, sel):
            try:
                if el.is_displayed():
                    _safe_click(driver, el)
                    return True
            except Exception:  # noqa: BLE001
                continue
    return False


def _click_vc_day(driver, date, max_nav=30):
    """Click a V-Calendar day by its exact 'id-YYYY-MM-DD' class (paging the
    calendar toward the target month if it isn't currently shown, and retrying
    while the grid renders / the open animation settles). Prefers the real
    in-month cell over an adjacent-month spillover cell of the same date."""
    iso = date.strftime("%Y-%m-%d")
    xp_in = ("//*[contains(concat(' ',normalize-space(@class),' '),' id-{iso} ')]"
             "[not(contains(concat(' ',normalize-space(@class),' '),' is-not-in-month '))]"
             ).format(iso=iso)
    xp_any = "//*[contains(concat(' ',normalize-space(@class),' '),' id-{iso} ')]".format(iso=iso)
    for _ in range(max_nav):
        for xp in (xp_in, xp_any):
            for el in driver.find_elements(By.XPATH, xp):
                try:
                    if not el.is_displayed():
                        continue
                    target = el
                    spans = el.find_elements(By.CSS_SELECTOR, ".vc-day-content")
                    if spans and spans[0].is_displayed():
                        target = spans[0]
                    _force_click(driver, target)  # bypass any intercepting overlay
                    return True
                except Exception:  # noqa: BLE001
                    continue
        rendered = _vc_rendered_dates(driver)
        if rendered and date < min(rendered):
            _vc_click_arrow(driver, "left")
        elif rendered and date > max(rendered):
            _vc_click_arrow(driver, "right")
        # else: in-range (or nothing rendered yet) -> the grid may still be
        # rendering / the open animation settling, so wait and retry rather than
        # giving up (this is what made a day click miss under a slow render).
        time.sleep(0.4)
    return False


def _try_calendar_range(driver, field, start, end):
    """Open the date popup and select start..end. VHP uses V-Calendar in range
    mode, whose day cells carry an exact 'id-YYYY-MM-DD' class — so we click days
    by date (no month-name/locale guessing). Falls back to a Quasar QDate for any
    other report that might use one."""
    popup, dump = _open_and_capture_popup(driver, field)
    print("    (period) popup opened on click: {}".format(dump))
    if popup is None:
        _close_overlays(driver)
        return False
    is_vcal = ("vc-day" in (dump or "")) or bool(driver.find_elements(By.CSS_SELECTOR, ".vc-day"))
    if is_vcal:
        time.sleep(0.7)  # let the open (slide-fade) animation settle before clicking
        # Range mode = click the start day, then the end day.
        if not _click_vc_day(driver, start):
            print("    (period) could not click start {}".format(start.strftime("%Y-%m-%d")))
        time.sleep(0.6)
        if not _click_vc_day(driver, end):
            print("    (period) could not click end {}".format(end.strftime("%Y-%m-%d")))
    else:  # fallback: standard Quasar QDate
        for label, d in (("start", start), ("end", end)):
            _qdate_goto(driver, d.year, d.month)
            if not _click_qday(driver, d.day):
                print("    (period) could not click {} day {}".format(label, d.day))
    time.sleep(0.5)
    _close_overlays(driver)
    return True


def _try_js_set_range(driver, field, start, end):
    """Last resort: set VISIBLE input value(s) via the native setter + Vue events.
    Unreliable for a {from,to} range model, so only ever trusted via read-back."""
    s = start.strftime(_VHP_DATE_FMT)
    e = end.strftime(_VHP_DATE_FMT)
    inputs = _visible_inputs(field)
    if not inputs:
        return False
    try:
        if len(inputs) >= 2:
            driver.execute_script(_JS_SET_INPUT, inputs[0], s)
            driver.execute_script(_JS_SET_INPUT, inputs[1], e)
        else:
            driver.execute_script(_JS_SET_INPUT, inputs[0], "{} - {}".format(s, e))
    except Exception:  # noqa: BLE001
        return False
    time.sleep(0.6)
    return True


def _wait_date_field(driver, timeout=15):
    """Poll for the 'Date' field to render (the Quasar filter panel can hydrate
    AFTER the SEARCH button that open_report waits on). Returns the field or None."""
    end = time.time() + timeout
    while time.time() < end:
        f = _find_date_field(driver)
        if f is not None:
            return f
        time.sleep(0.5)
    return None


def _field_labels(driver):
    """Every visible q-field label on the page — printed when the 'Date' field
    isn't found, so the right control (e.g. a 'Month' picker) is identifiable."""
    out = []
    for el in driver.find_elements(By.CSS_SELECTOR, ".q-field__label"):
        try:
            if el.is_displayed():
                t = (el.text or "").strip()
                if t:
                    out.append(t)
        except Exception:  # noqa: BLE001
            continue
    return out


def _inputs_summary(field):
    """Readable summary of the field's visible inputs (readonly? placeholder?),
    so typeable-vs-calendar-only is clear from one run's log."""
    parts = []
    for i in _visible_inputs(field):
        try:
            ro = "readonly" if i.get_attribute("readonly") else "editable"
            ph = (i.get_attribute("placeholder") or "")[:24]
            parts.append("[{} ph={!r}]".format(ro, ph))
        except Exception:  # noqa: BLE001
            continue
    if not parts:
        return "0 visible inputs (calendar-only — will use the QDate popup)"
    return "{} input(s): {}".format(len(parts), " ".join(parts))


# ---------------------------------------------------------------------------
# Element-UI month picker (the yearly-forecast report's start-month control)
# ---------------------------------------------------------------------------
# The yearly-forecast report shows 12 months starting from this month. It's an
# Element-UI (el-date-editor--month) field, data-cy='field-month', shown as
# "MM/YYYY" (e.g. "10/2026"), and the input is editable — so we type the month in.
_MONTH_CY = "field-month"


def _month_input(driver):
    """The editable <input> of the Element-UI month picker, or None."""
    for css in ("[data-cy='{}'] input".format(_MONTH_CY),
                "[data-cy='{}'] .el-input__inner".format(_MONTH_CY),
                "input.el-input__inner"):
        for e in driver.find_elements(By.CSS_SELECTOR, css):
            try:
                if e.is_displayed():
                    return e
            except Exception:  # noqa: BLE001
                continue
    return None


def _month_value(driver, inp=None):
    inp = inp or _month_input(driver)
    if inp is None:
        return ""
    try:
        return (inp.get_attribute("value") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _month_matches(shown, target):
    """True if `shown` (e.g. '01/2026' or '2026-01') is target's month+year."""
    m = re.search(r"(\d{1,2})\s*[/\-]\s*(\d{4})", shown or "")
    if m:
        return int(m.group(1)) == target.month and int(m.group(2)) == target.year
    m = re.search(r"(\d{4})\s*[/\-]\s*(\d{1,2})", shown or "")  # YYYY-MM form
    if m:
        return int(m.group(1)) == target.year and int(m.group(2)) == target.month
    return False


def _panel_year(driver):
    """The year currently shown in an open Element-UI month panel, or None."""
    for css in (".el-date-picker__header-label", ".el-picker-panel__header label",
                ".el-date-picker__header", ".el-picker-panel__header"):
        for e in driver.find_elements(By.CSS_SELECTOR, css):
            try:
                if e.is_displayed():
                    m = re.search(r"(\d{4})", e.text or "")
                    if m:
                        return int(m.group(1))
            except Exception:  # noqa: BLE001
                continue
    return None


def _set_month_via_panel(driver, target):
    """Fallback: open the month panel, page the year to target, click the month
    cell by position (Jan..Dec = cells 0..11, locale-independent)."""
    inp = _month_input(driver)
    if inp is None:
        return False
    _safe_click(driver, inp)
    if not _wait_css(driver, ".el-month-table, .el-date-picker, .el-picker-panel", 2.0):
        return False
    for _ in range(30):
        yr = _panel_year(driver)
        if yr is None or yr == target.year:
            break
        arrow = ".el-icon-d-arrow-left" if target.year < yr else ".el-icon-d-arrow-right"
        btns = [b for b in driver.find_elements(By.CSS_SELECTOR, arrow) if _disp_safe(b)]
        if not btns:
            break
        _safe_click(driver, btns[0])
        time.sleep(0.2)
    cells = [c for c in driver.find_elements(By.CSS_SELECTOR, ".el-month-table td")
             if _disp_safe(c)]
    if len(cells) >= 12:
        _force_click(driver, cells[target.month - 1])
        time.sleep(0.4)
        _close_overlays(driver)
        return True
    return False


def _set_month_picker(driver, target):
    """Set the yearly-forecast month picker to target's month+year (shown MM/YYYY).
    Types the value into the editable input first, then falls back to the panel.
    Returns True once the field reads back as target."""
    want = "{:02d}/{:04d}".format(target.month, target.year)
    # the control can hydrate slightly after the page's Search button appears
    end = time.time() + 15
    inp = None
    while time.time() < end:
        inp = _month_input(driver)
        if inp is not None:
            break
        time.sleep(0.5)
    if inp is None:
        print("    (month) no month picker (data-cy='{}') found. Field labels: {}"
              .format(_MONTH_CY, ", ".join(_field_labels(driver)) or "(none)"))
        print("    (month) page diag:\n{}".format(_dump_date_diag(driver)))
        return False

    print("    (month) want {} | field now shows: {}".format(want, _month_value(driver, inp) or "(blank)"))

    # Strategy 1: type MM/YYYY into the editable input, commit with Enter.
    try:
        inp.click()
        time.sleep(0.2)
        inp.send_keys(Keys.CONTROL, "a")
        inp.send_keys(Keys.DELETE)
        inp.send_keys(want)
        time.sleep(0.3)
        inp.send_keys(Keys.ENTER)
        time.sleep(0.7)
    except Exception as exc:  # noqa: BLE001
        print("    (month) typing failed: {}".format(_describe(exc)))
    _close_overlays(driver)
    shown = _month_value(driver)
    if _month_matches(shown, target):
        print("    (month) set to {} by typing (field shows: {})".format(want, shown))
        return True

    # Strategy 2: the panel (year arrows + month cell by position).
    print("    (month) typing didn't stick (shows {}); trying the panel".format(shown or "(blank)"))
    try:
        if _set_month_via_panel(driver, target):
            shown = _month_value(driver)
            if _month_matches(shown, target):
                print("    (month) set to {} via panel (field shows: {})".format(want, shown))
                return True
    except Exception as exc:  # noqa: BLE001
        print("    (month) panel strategy error: {}".format(_describe(exc)))

    print("    (month) WARNING could not set month to {} (field shows {}). Send me "
          "this block and I'll adjust.".format(want, _month_value(driver) or "(blank)"))
    return False


def set_period(driver, report):
    """Set the report's Date filter before SEARCH (e.g. the last 7 days for the
    weekly reservation report). NON-FATAL wrapper: any error is swallowed (we
    leave VHP's default and carry on) and overlays are cleaned up."""
    try:
        return _set_period_inner(driver, report)
    except Exception as exc:  # noqa: BLE001 - must never abort the scrape
        print("    (period) error (ignored, leaving VHP default): {}".format(_describe(exc)))
        try:
            _close_overlays(driver)
        except Exception:  # noqa: BLE001
            pass
        return False


def _set_period_inner(driver, report):
    """Self-diagnosing date setter: prints the Date field HTML + before/after
    values, then tries (1) typing into an editable input, (2) driving the QDate
    calendar popup, (3) a JS value-set, verifying each by reading the field back.
    Returns True once the shown range matches. Reports with no Date field (period
    None, or no such control) are a safe no-op."""
    p = report.get("period")
    # Month-picker reports (yearly-forecast): drive the Element-UI month field.
    #   "calendar_year"           -> January of the CURRENT year
    #   "next_calendar_year"      -> January of NEXT year (dynamic)
    #   "current_month_this_year" -> THIS month, current year (dynamic; e.g. run in
    #                                Oct 2026 -> 10/2026 = a rolling 12-month FORWARD
    #                                view from the current month). Advances monthly.
    #   "current_month_next_year" -> THIS month, of NEXT year (dynamic; 10/2027)
    #   {"month": "YYYY-MM"}      -> that exact month (fixed)
    target = None
    if p == "calendar_year":
        target = datetime.date.today().replace(month=1, day=1)
    elif p == "current_month_this_year":
        target = datetime.date.today().replace(day=1)
    elif p == "next_calendar_year":
        target = datetime.date(datetime.date.today().year + 1, 1, 1)
    elif p == "current_month_next_year":
        t = datetime.date.today()
        target = datetime.date(t.year + 1, t.month, 1)
    elif isinstance(p, dict) and p.get("month") and not (p.get("start") and p.get("end")):
        try:
            target = _parse_iso("{}-01".format(p["month"]))
        except Exception as exc:  # noqa: BLE001
            print("    (period) bad month {!r}: {}".format(p.get("month"), _describe(exc)))
            return False
    if target is not None:
        ok = _set_month_picker(driver, target)
        _close_overlays(driver)
        return ok

    try:
        rng = _period_range(report)
    except Exception as exc:  # noqa: BLE001 - bad period config must not abort
        print("    (period) bad period config {!r}: {}".format(report.get("period"), _describe(exc)))
        return False
    if rng is None:
        return True  # nothing to set for this report
    start, end = rng
    want = "{} - {}".format(start.strftime(_VHP_DATE_FMT), end.strftime(_VHP_DATE_FMT))

    field = _wait_date_field(driver, 15)
    if field is None:
        print("    (period) no 'Date' field found (waited 15s). Visible field labels: {}"
              .format(", ".join(_field_labels(driver)) or "(none)"))
        print("    (period) page diag:\n{}".format(_dump_date_diag(driver)))
        return False

    before = _date_field_value(driver, field)
    print("    (period) want {} | field now shows: {}".format(want, before or "(blank)"))
    print("    (period) inputs: {}".format(_inputs_summary(field)))
    print("    (period) field HTML: {}".format(_el_html(driver, field)))

    def _shown():
        time.sleep(0.4)  # let Quasar re-render before trusting a read-back
        return _date_field_value(driver, _find_date_field(driver) or field)

    # NOTE: a raw JS value-set is intentionally NOT used on this readonly
    # {from,to} range input — it only half-updates the model (sets 'from', leaves
    # 'to'), producing a wrong search range. The calendar popup is the real path.
    strategies = (
        ("type", _try_type_range),          # editable input only (readonly -> no-op)
        ("calendar", _try_calendar_range),  # the reliable path for a readonly range
    )
    for name, fn in strategies:
        field = _find_date_field(driver) or field  # re-find: a prior strategy may have re-rendered
        try:
            fn(driver, field, start, end)
        except Exception as exc:  # noqa: BLE001
            print("    (period) {} strategy error: {}".format(name, _describe(exc)))
            _close_overlays(driver)
            continue
        shown = _shown()
        if _range_matches(shown, start, end):
            print("    (period) set to {} via {} (field shows: {})".format(want, name, shown))
            _close_overlays(driver)
            return True
        print("    (period) after {}: field shows {} (not yet {})".format(name, shown or "(blank)", want))
        _close_overlays(driver)

    final = _date_field_value(driver, _find_date_field(driver) or field)
    print("    (period) WARNING could not set range — field shows {} (wanted {}). Send me "
          "the 'field HTML' and 'QDate popup HTML' lines above and I'll finish it.".format(
              final or "(blank)", want))
    return False


def _select_radio(driver, label):
    """Select a Quasar radio (role='radio') by its aria-label OR visible text
    (e.g. 'Cancel Date' vs 'Check-in Date'). Clicks and VERIFIES that aria-checked
    actually flips to true (retrying, and trying the radio's inner elements, since
    a bare click on the wrapper can be ignored). Prints a diagnostic of the radios
    it saw if it can't match. Returns True on success."""
    want = " ".join(label.strip().upper().split())

    def _radios():
        return [el for el in driver.find_elements(By.XPATH, "//*[@role='radio']")
                if _disp_safe(el)]

    # The filter panel can hydrate a moment after the SEARCH button appears.
    end = time.time() + 15
    while time.time() < end and not _radios():
        time.sleep(0.4)

    def _label_of(el):
        try:
            al = " ".join((el.get_attribute("aria-label") or "").upper().split())
            tx = " ".join((el.text or "").upper().split())
            return al, tx
        except Exception:  # noqa: BLE001
            return "", ""

    def _find():
        for el in _radios():
            al, tx = _label_of(el)
            if want == al or want == tx or (al and want in al) or (tx and want in tx):
                return el
        return None

    def _checked(el):
        try:
            return (el.get_attribute("aria-checked") or "").lower() == "true"
        except Exception:  # noqa: BLE001
            return False

    target = _find()
    if target is None:
        print("    (config) radio {!r} NOT FOUND — radios seen: {}".format(
            label, [_label_of(el) for el in _radios()]))
        return False
    if _checked(target):
        return True

    candidates = [target]
    try:
        candidates += target.find_elements(
            By.CSS_SELECTOR, ".q-radio__inner, .q-radio__bg, .q-radio__label")
    except Exception:  # noqa: BLE001
        pass
    for _ in range(3):
        for c in candidates:
            try:
                _force_click(driver, c)
            except Exception:  # noqa: BLE001
                continue
            time.sleep(0.4)
            tgt = _find() or target
            if _checked(tgt):
                return True
    return _checked(_find() or target)


def select_date_mode(driver, report):
    """If the report names a date-type radio (report['select_radio'], e.g.
    'Cancel Date' on the cancellation report), select it BEFORE the date range is
    set so the range filters the intended date. Returns True if selected (or none
    needed), False if it was needed but couldn't be selected — the caller gates on
    that so we never upload a CSV filtered by the wrong date."""
    label = report.get("select_radio")
    if not label:
        return True
    try:
        ok = _select_radio(driver, label)
        print("    (config) date-mode radio {!r} -> {}".format(
            label, "selected" if ok else "NOT selected (see diagnostic above)"))
        return ok
    except Exception as exc:  # noqa: BLE001
        print("    (config) date-mode radio {!r}: {}".format(label, _describe(exc)))
        return False


def configure_report(driver, report):
    """Best-effort: apply the report's filter settings before SEARCH.

    Optional report fields in config.py:
        dropdowns:       {"Reservation Status": "Guaranteed", "Room Type": "All"}
                                                     -> set each single-select
        checkboxes:      {"Show Revenue": True}      -> set each checkbox
        segment_untick:  ["Complimentary", "House Use"]  -> de-select in Segment Code

    Never raises — it prints what it did (or couldn't) and the scrape carries on.
    """
    if not (report.get("dropdowns") or report.get("checkboxes")
            or report.get("segment_untick")):
        return
    # Wait for the left filter panel to render (it can lag behind SEARCH).
    end = time.time() + 20
    while time.time() < end:
        if (driver.find_elements(By.CSS_SELECTOR, "[data-cy='{}']".format(_SEGMENT_CY))
                or driver.find_elements(By.XPATH, "//*[contains(@class,'q-checkbox')]")
                or driver.find_elements(By.CSS_SELECTOR, "label.q-select, label.q-field")):
            break
        time.sleep(0.5)

    for label, value in (report.get("dropdowns") or {}).items():
        try:
            changed = _select_dropdown(driver, label, value)
            print("    (config) dropdown {!r} -> {!r}{}".format(
                label, value, "" if changed else " (already)"))
        except Exception as exc:  # noqa: BLE001 - best-effort; default usually matches
            print("    (config) dropdown {!r} -> {!r}: {}".format(
                label, value, _describe(exc)))

    for label, want in (report.get("checkboxes") or {}).items():
        try:
            changed = _set_checkbox(driver, label, bool(want))
            print("    (config) checkbox {!r} -> {}{}".format(
                label, "on" if want else "off", "" if changed else " (already)"))
        except Exception as exc:  # noqa: BLE001
            print("    (config) checkbox {!r}: {}".format(label, _describe(exc)))

    labels = report.get("segment_untick") or []
    if labels:
        try:
            _segment_untick(driver, labels)
        except Exception as exc:  # noqa: BLE001
            print("    (config) Segment Code untick {}: {}".format(labels, _describe(exc)))


def _pick_field(hits):
    """Of several matching field wrappers, the one with the LEAST text — i.e. the
    innermost/most specific match (just label + value, not surrounding panel)."""
    best = None
    best_len = None
    for el in hits:
        try:
            tlen = len((el.text or ""))
        except Exception:  # noqa: BLE001
            tlen = 10 ** 9
        if best is None or tlen < best_len:
            best, best_len = el, tlen
    return best


def _dropdown_field(driver, label):
    """Find the single-select Quasar dropdown (q-select) whose label matches
    `label` (e.g. 'Reservation Status'). Returns the clickable field or None."""
    want = label.upper()
    for cls in ("q-select", "q-field"):
        xp = ("//label[contains(@class,'{cls}')]"
              "[contains(translate(normalize-space(.),'{lo}','{up}'), {w!r})]"
              ).format(cls=cls, lo=_LOWER, up=_UPPER, w=want)
        hits = [e for e in driver.find_elements(By.XPATH, xp) if _disp_safe(e)]
        if hits:
            return _pick_field(hits)
    return None


def _dropdown_value(driver, field):
    try:
        return (field.text or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _dropdown_labels_diag(driver):
    """Print the dropdown field labels actually on the page — so when a label isn't
    found we can see what to target (same idea as the radio/segment diagnostics)."""
    try:
        labels = driver.execute_script(
            "var out=[];document.querySelectorAll("
            "'label.q-select .q-field__label, label.q-field .q-field__label')"
            ".forEach(function(e){var t=(e.textContent||'').trim();if(t)out.push(t);});"
            "return out.slice(0,40);")
        print("    (config) dropdowns on page: {}".format(labels))
    except Exception:  # noqa: BLE001
        pass


def _select_dropdown(driver, label, value):
    """Set a single-select Quasar dropdown (found by its label) to `value`
    (e.g. 'Reservation Status' -> 'Guaranteed'). Returns True if it changed, False
    if already set. Raises RuntimeError if the field/option can't be found (the
    caller logs it; the scrape carries on — the default usually already matches)."""
    field = _dropdown_field(driver, label)
    if field is None:
        _dropdown_labels_diag(driver)
        raise RuntimeError("dropdown {!r} not found".format(label))
    if value.upper() in _dropdown_value(driver, field).upper():
        return False  # already showing the wanted value
    # Open it (retry until the q-menu renders).
    opened = False
    for _ in range(3):
        _safe_click(driver, field)
        for _ in range(12):
            if driver.find_elements(By.CSS_SELECTOR, ".q-menu .q-item"):
                opened = True
                break
            time.sleep(0.2)
        if opened:
            break
    opt = _find_option(driver, value) if opened else None
    if opt is None:
        try:
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError("option {!r} not found in dropdown {!r}".format(value, label))
    _safe_click(driver, opt)
    time.sleep(0.4)
    # Close any lingering menu so it can't bleed into SEARCH.
    if any(_disp_safe(m) for m in driver.find_elements(By.CSS_SELECTOR, ".q-menu")):
        try:
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        except Exception:  # noqa: BLE001
            pass
    # Verify it stuck.
    if value.upper() not in _dropdown_value(driver, field).upper():
        raise RuntimeError(
            "dropdown {!r}: picked {!r} but field still shows {!r}".format(
                label, value, _dropdown_value(driver, field)[:40]))
    return True


def _set_checkbox(driver, label, want):
    """Set a Quasar checkbox (found by its visible label) to want (bool).
    Returns True if it changed, False if already in the wanted state."""
    target = None
    for el in driver.find_elements(
        By.XPATH,
        "//*[contains(@class,'q-checkbox')][contains({}, {!r})]".format(_ci("."), label.upper()),
    ):
        if el.is_displayed():
            target = el
            break
    if target is None:
        raise RuntimeError("checkbox not found")
    checked = (target.get_attribute("aria-checked") or "").lower() == "true"
    if checked == want:
        return False
    _safe_click(driver, target)
    return True


_SEGMENT_CY = "dropdown-segment-code"


def _segment_value(driver):
    """The Segment Code field's shown value (e.g. 'ALL' or 'FIT, FIT-OTA, …')."""
    for css in (
        "[data-cy='{}'] .ellipsis".format(_SEGMENT_CY),
        "[data-cy='{}']".format(_SEGMENT_CY),
    ):
        for el in driver.find_elements(By.CSS_SELECTOR, css):
            if el.is_displayed():
                txt = (el.text or el.get_attribute("textContent") or "").strip()
                if txt:
                    return txt
    return ""


def _segment_untick(driver, exclude_labels):
    """Set Segment Code to everything EXCEPT the given option labels.

    Opens the data-cy='dropdown-segment-code' multi-select, makes sure all are
    selected (deterministic start), then de-selects each excluded option. The
    list is virtually scrolled, so options are found by scrolling the menu.
    """
    field = None
    for el in driver.find_elements(By.CSS_SELECTOR, "[data-cy='{}']".format(_SEGMENT_CY)):
        if el.is_displayed():
            field = el
            break
    if field is None:
        raise RuntimeError("Segment Code field (data-cy) not found")

    # Open the dropdown; retry until its options actually render (slower PCs /
    # longer code lists can lag behind the click).
    for _ in range(3):
        _safe_click(driver, field)
        opened = False
        for _ in range(12):
            if driver.find_elements(By.CSS_SELECTOR, ".q-menu .q-item"):
                opened = True
                break
            time.sleep(0.2)
        if opened:
            break
    time.sleep(0.3)

    # Deterministic start: make sure everything is selected.
    if _segment_value(driver).upper() != "ALL":
        allopt = _find_option(driver, "All")
        if allopt is not None:
            _safe_click(driver, allopt)
            time.sleep(0.7)

    missing = []
    for lbl in exclude_labels:
        item = _find_option(driver, lbl)
        if item is None:
            missing.append(lbl)
            print("    (config) Segment Code: {!r} not found".format(lbl))
            continue
        _safe_click(driver, item)
        print("    (config) Segment Code: de-selected {!r}".format(lbl))
        time.sleep(0.4)

    if missing:  # diagnostic: is the menu even open, and what labels are there?
        try:
            labels = driver.execute_script(
                "var out=[];document.querySelectorAll('.q-menu .q-item__label').forEach("
                "function(e){var t=(e.textContent||'').trim();if(t){out.push(t);}});"
                "return out.slice(0,60);")
            menu_open = any(_disp_safe(m) for m in driver.find_elements(By.CSS_SELECTOR, ".q-menu"))
            print("    (config) segment diag: menu_open={} | options seen: {}".format(menu_open, labels))
        except Exception:  # noqa: BLE001
            pass

    # Close the dropdown fully so it can't bleed into SEARCH or the export menu.
    for _ in range(3):
        try:
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
        if not any(_disp_safe(m) for m in driver.find_elements(By.CSS_SELECTOR, ".q-menu")):
            break
        try:  # still open -> toggle the field closed
            _safe_click(driver, field)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
    print("    (config) Segment Code now shows: {}".format(_segment_value(driver)[:70] or "(blank)"))


# Scroll the open q-menu's actual scrollable list. arguments: (delta, absolute).
# Finds the real scroll container (the descendant that overflows) rather than
# guessing a class, so a long virtual-scroll list pages correctly.
_SCROLL_MENU_JS = (
    "var menus=document.querySelectorAll('.q-menu'), menu=null;"
    "for(var i=0;i<menus.length;i++){var r=menus[i].getBoundingClientRect();"
    "if(r.width>0&&r.height>0){menu=menus[i];break;}}"
    "if(!menu){return -1;}"
    "function sc(root){var a=[root].concat([].slice.call(root.querySelectorAll('*')));"
    "for(var j=0;j<a.length;j++){if(a[j].scrollHeight-a[j].clientHeight>4){return a[j];}}return root;}"
    "var s=sc(menu);"
    "if(arguments[1]){s.scrollTop=arguments[0];}else{s.scrollTop=s.scrollTop+arguments[0];}"
    "return s.scrollTop;"
)


def _find_option(driver, label):
    """Find a dropdown option whose q-item label EXACTLY equals `label`,
    sweeping the open menu's real scrollable list (virtual scroll) top-to-bottom.
    Returns the clickable q-item row (scrolled into view), or None."""
    want = label.upper()
    xp = (
        "//div[contains(@class,'q-item')]"
        "[.//div[contains(@class,'q-item__label')]"
        "[translate(normalize-space(.),'{lo}','{up}')={w!r}]]"
    ).format(lo=_LOWER, up=_UPPER, w=want)

    def _hit():
        for el in driver.find_elements(By.XPATH, xp):
            try:
                if el.is_displayed():
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                    return el
            except Exception:  # noqa: BLE001
                continue
        return None

    # Wait for options to render after the menu opens (slower PCs / longer lists).
    for _ in range(12):
        if driver.find_elements(By.CSS_SELECTOR, ".q-menu .q-item"):
            break
        time.sleep(0.2)

    driver.execute_script(_SCROLL_MENU_JS, 0, True)  # start at the top
    time.sleep(0.2)
    last, stalls = -1.0, 0
    for _ in range(80):
        hit = _hit()
        if hit is not None:
            return hit
        try:
            pos = float(driver.execute_script(_SCROLL_MENU_JS, 240, False))
        except Exception:  # noqa: BLE001
            pos = -1.0
        time.sleep(0.2)
        if pos == last:  # list didn't advance; allow a couple of lazy-render stalls
            stalls += 1
            if stalls >= 3:
                break
        else:
            stalls = 0
        last = pos
    return _hit()


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


def _visible_menu_items(driver):
    """Return [(element, UPPER_TEXT, raw_text)] for each visible row of an open
    Quasar menu. Prefers rows inside the floating .q-menu, falling back to any
    visible q-item only if that finds nothing."""
    for xp in _MENU_ITEM_XPATHS:
        out, seen = [], set()
        for el in driver.find_elements(By.XPATH, xp):
            try:
                if not el.is_displayed():
                    continue
                if el.id in seen:
                    continue
                seen.add(el.id)
                txt = (el.text or el.get_attribute("textContent") or "").strip()
                out.append((el, txt.upper(), txt))
            except Exception:  # noqa: BLE001 - stale element, skip
                continue
        if out:
            return out
    return []


def _pick_csv_item(items):
    """Choose the 'Print CSV' row from menu items: exact 'PRINT CSV' first, then
    a row mentioning both PRINT and CSV, then any row mentioning CSV."""
    for el, up, _ in items:
        if up == "PRINT CSV":
            return el
    for el, up, _ in items:
        if "PRINT" in up and "CSV" in up:
            return el
    for el, up, _ in items:
        if "CSV" in up:
            return el
    return None


def export_print_csv(driver):
    """Open the printer menu and click 'Print CSV' to start the download.

    Clicks the toolbar icon buttons (printer is the right-most) until a menu
    with a CSV export row appears, then clicks it. Matches the row by its text
    (any row mentioning CSV) rather than one fixed label, so it works across
    reports whose menus word the entry slightly differently, and it prints the
    menu rows it actually saw so a miss is diagnosable.
    """
    buttons = _toolbar_buttons(driver)
    if not buttons:
        raise RuntimeError("print/refresh toolbar not found above the table")
    last_err = None
    all_seen = []
    dismiss_blocking_dialog(driver)  # a modal backdrop would block every button
    for btn in reversed(buttons):  # printer is the right-most icon; try it first
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        except Exception:  # noqa: BLE001
            pass
        try:
            # _safe_click's JS fallback fires the click even if a dialog backdrop
            # is intercepting normal clicks ('not clickable at point').
            _safe_click(driver, btn)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue
        # Let the menu animate in; poll so a slow-rendering menu isn't missed.
        items, target, deadline = [], None, time.time() + 3.0
        while time.time() < deadline:
            items = _visible_menu_items(driver)
            target = _pick_csv_item(items)
            if target is not None or items:
                break
            time.sleep(0.2)
        labels = [t for (_, _, t) in items if t]
        if labels:
            all_seen.extend(labels)
            print("    (export) menu: {}".format(" | ".join(labels[:12])))
        if target is not None:
            _safe_click(driver, target)
            return
        try:  # close the menu we opened before trying the next button
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
    uniq = list(dict.fromkeys(all_seen))
    raise RuntimeError(
        "'Print CSV' not found ({} button(s) tried). Menu rows seen: {}{}".format(
            len(buttons),
            "; ".join(uniq) if uniq else "(no menu opened)",
            "" if last_err is None else " | last click error: {}".format(last_err),
        )
    )


# ---------------------------------------------------------------------------
# download capture
# ---------------------------------------------------------------------------
def _norm_folders(folders):
    if folders is None:
        folders = config.DOWNLOAD_WATCH
    if isinstance(folders, str):
        folders = [folders]
    folders = [f for f in dict.fromkeys(folders) if f]  # de-dupe, keep order
    for f in folders:
        try:
            os.makedirs(f, exist_ok=True)
        except Exception:  # noqa: BLE001 - e.g. a user folder we can't create
            pass
    return folders


def _listdir(folder):
    try:
        return os.listdir(folder)
    except Exception:  # noqa: BLE001
        return []


def _is_temp_download(name):
    n = name.lower()
    return n.endswith(".crdownload") or n.endswith(".tmp") or n.endswith(".partial")


def snapshot_dirs(folders=None):
    """Record each folder's current files, to call BEFORE triggering a download
    so even an instant download is detected as new."""
    folders = _norm_folders(folders)
    return {f: set(_listdir(f)) for f in folders}


def wait_for_download(folders=None, timeout=None, before=None, since=None):
    """Return the path of a newly finished download. Detected two ways, so a
    same-name re-download isn't missed:
      1. a file NOT in the `before` snapshot (unique-named exports), and
      2. any <EXPORT_EXT> file whose mtime is at/after `since` — this catches a
         report (e.g. the yearly-forecast 'Print CSV') that writes the SAME
         filename every run, which a name-only diff would never see as 'new'.
    Chrome writes a .crdownload until the file is complete, so temp files are
    skipped. On timeout it lists the newest files it DID see, to reveal where the
    download actually landed."""
    if timeout is None:
        timeout = config.DOWNLOAD_TIMEOUT
    folders = _norm_folders(folders)
    if before is None:
        before = {f: set(_listdir(f)) for f in folders}
    if since is None:
        since = time.time() - 2  # small clock-skew allowance
    want_ext = "." + config.EXPORT_EXT.lower().lstrip(".")
    end = time.time() + timeout
    while time.time() < end:
        for f in folders:
            cands = []
            for x in _listdir(f):
                if _is_temp_download(x):
                    continue
                p = os.path.join(f, x)
                is_new = x not in before.get(f, set())
                try:
                    fresh = x.lower().endswith(want_ext) and os.path.getmtime(p) >= since
                except OSError:
                    fresh = False
                if is_new or fresh:
                    cands.append(p)
            if cands:
                cands.sort(key=lambda p: os.path.getmtime(p), reverse=True)
                return cands[0]
        time.sleep(1)
    # Timeout: show the newest few files in each watched folder for diagnosis.
    diag = []
    for f in folders:
        rows = []
        for x in _listdir(f):
            try:
                rows.append((os.path.getmtime(os.path.join(f, x)), x))
            except OSError:
                continue
        rows.sort(reverse=True)
        diag.append("{} -> {}".format(f, ", ".join(n for _, n in rows[:5]) or "(empty)"))
    raise TimeoutError(
        "download did not finish within {}s. Newest files seen: {}".format(
            timeout, " | ".join(diag)))


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
def _describe(exc):
    """One-line, readable description of an exception for logs."""
    first = (str(exc).strip().splitlines() or [""])[0]
    return "{}: {}".format(type(exc).__name__, first or "(no details)")


def scrape_report(driver, prop, report, download_dir):
    """Run the full flow for one report; return the saved CSV path.

    Each step names itself in any error (so a failure reads like
    "[export Print CSV] TimeoutException: ...") and main.py/test_scrape.py can
    log it and carry on with the next report.
    """
    def step(name, fn):
        try:
            return fn()
        except LoginRequired:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("[{}] {}".format(name, _describe(exc)))

    step("open report", lambda: open_report(driver, report))
    dismiss_blocking_dialog(driver)  # clear a stale modal backdrop that blocks clicks
    radio_ok = select_date_mode(driver, report)  # pick the date-type radio BEFORE the date
    if report.get("select_radio") and not radio_ok:
        raise RuntimeError(
            "[date mode] required radio {!r} not selected — skipping report to avoid "
            "a CSV filtered by the wrong date".format(report.get("select_radio")))
    period_ok = set_period(driver, report)  # best-effort date filter (non-fatal; prints)
    if report.get("period_required") and not period_ok:
        # Opt-in safety gate (default OFF). Once set_period is confirmed working
        # for a report, set "period_required": True in config so a date range
        # that can't be set AND verified SKIPS the report instead of exporting a
        # CSV with VHP's default (wrong) window — i.e. never upload bad data.
        raise RuntimeError(
            "[set period] required date range not set/verified — skipping report "
            "to avoid wrong-period data")
    configure_report(driver, report)  # best-effort filter settings (non-fatal)
    step("click SEARCH", lambda: click_search(driver))
    step("wait for data", lambda: wait_for_report_data(driver))
    time.sleep(1)  # let the grid settle before exporting
    # Re-assert the download directory (page navigation can reset it).
    for _cmd in ("Browser.setDownloadBehavior", "Page.setDownloadBehavior"):
        try:
            driver.execute_cdp_cmd(_cmd, {"behavior": "allow", "downloadPath": download_dir})
        except Exception:  # noqa: BLE001
            pass
    _before = snapshot_dirs(config.DOWNLOAD_WATCH)  # snapshot BEFORE the click
    _since = time.time()                            # mtime floor for the download
    step("export Print CSV", lambda: export_print_csv(driver))
    raw = step("wait for download",
               lambda: wait_for_download(config.DOWNLOAD_WATCH, before=_before, since=_since))
    return rename_download(raw, prop["code"], report["name"], download_dir)
