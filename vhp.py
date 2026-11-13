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
    """Best-effort ROBUST clear of inputs the bot itself typed into (so a failed
    auto-login doesn't leave stale digits/creds for a human to submit by reflex).
    Selenium's .clear() is silently ignored by some Vue/Quasar inputs, so also do
    select-all + delete. Never raises; the page may have navigated out."""
    for el in els or []:
        try:
            el.clear()
            if (el.get_attribute("value") or ""):
                el.send_keys(Keys.CONTROL, "a")
                el.send_keys(Keys.DELETE)
        except Exception:  # noqa: BLE001 - stale / navigated away
            pass


def _clear_input(driver, el):
    """Robustly EMPTY one input — even a browser-AUTOFILLED or Vue/Quasar-bound one
    where Selenium's .clear() is ignored (which is how the login creds got typed ON
    TOP of the autofill and concatenated into 'useruser...'). Tries .clear(), then
    select-all + delete, then a JS value='' with the input/change events Vue listens
    on, verifying empty after each. Never raises."""
    def _val():
        try:
            return el.get_attribute("value") or ""
        except Exception:  # noqa: BLE001
            return ""
    try:
        el.clear()
    except Exception:  # noqa: BLE001
        pass
    if not _val():
        return
    try:
        el.send_keys(Keys.CONTROL, "a")
        el.send_keys(Keys.DELETE)
    except Exception:  # noqa: BLE001
        pass
    if not _val():
        return
    try:
        driver.execute_script(
            "var e=arguments[0]; e.value='';"
            "e.dispatchEvent(new Event('input',{bubbles:true}));"
            "e.dispatchEvent(new Event('change',{bubbles:true}));", el)
    except Exception:  # noqa: BLE001
        pass


def _set_login_field(driver, el, want):
    """Make `el` hold EXACTLY `want` (a username or password), handling browser
    autofill + Vue inputs so we NEVER append to a stale/autofilled value. If it is
    already == want (the browser filled the right value), leave it untouched. Returns
    True only if the field ends up exactly == want."""
    if el is None:
        return False

    def _val():
        try:
            return el.get_attribute("value") or ""
        except Exception:  # noqa: BLE001
            return ""

    if _val() == want:
        return True                      # already correct — don't retype (no concat)
    for _ in range(2):
        _clear_input(driver, el)
        try:
            el.send_keys(want)
        except Exception:  # noqa: BLE001
            pass
        if _val() == want:
            return True
    return _val() == want


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


def _refresh_captcha(driver):
    """Click VHP's small round refresh-CAPTCHA button (just LEFT of the digit
    canvas) to load a NEW CAPTCHA — WITHOUT reloading the page (keeps the filled
    login) and WITHOUT submitting. Returns True only if a new CAPTCHA appeared.
    Best-effort and safe: it never clicks a wide button (the LOG IN bar), only a
    small control on the CAPTCHA's own row and to its left; if it can't find one,
    it does nothing (the caller then just hands off)."""
    try:
        import captcha
        canvas = captcha._find_captcha_image(driver)
        if canvas is None:
            return False
        cx = (canvas.location or {}).get("x", 0)
        cmid = (canvas.location or {}).get("y", 0) + (canvas.size or {}).get("height", 40) / 2.0
        prev = captcha.canvas_png(driver, canvas)
        target = None
        for b in driver.find_elements(By.CSS_SELECTOR, "button, .q-btn, [role='button']"):
            try:
                if not b.is_displayed():
                    continue
                bw = (b.size or {}).get("width", 0)
                bx = (b.location or {}).get("x", 0)
                bmid = (b.location or {}).get("y", 0) + (b.size or {}).get("height", 0) / 2.0
                # small, same row, just to the LEFT of the digit canvas (the ↻ icon)
                if 0 < bw <= 60 and cx - 90 < bx < cx and abs(bmid - cmid) <= 28:
                    target = b
                    break
            except Exception:  # noqa: BLE001
                continue
        if target is None:
            return False
        _safe_click(driver, target)
        for _ in range(12):   # wait until the canvas pixels actually change
            time.sleep(0.3)
            now = captcha.canvas_png(driver, captcha._find_captcha_image(driver) or canvas)
            if now and now != prev:
                return True
        return False
    except Exception:  # noqa: BLE001 - refresh is best-effort; never raises
        return False


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
    _cnn_ok = False
    try:
        import captcha_cnn
        _cnn_ok = captcha_cnn.available()
    except Exception:  # noqa: BLE001
        _cnn_ok = False
    if not _cnn_ok and not captcha.ocr_available():
        _log("no CAPTCHA reader available — add captcha_cnn.npz (the trained model) "
             "or install Tesseract (`pip install pytesseract pillow`)")
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

    # Fill username/password. If we have stored credentials, TYPE them (clearing the
    # field first) so we overwrite any browser autofill with the known-correct
    # values — autofill can be stale/partial, and a blind send_keys would APPEND to
    # it. Without stored credentials, fall back to whatever the browser remembered.
    # Either way we verify both fields are non-empty before submitting. We track what
    # WE filled so we can clear it again on failure.
    user_el, pwd_el, submit_el = form
    creds = config.get_credentials(prop["code"])
    filled = []
    if creds:
        # Set each field to EXACTLY our stored value, handling autofill + Vue inputs
        # so we never type ON TOP of a remembered value and concatenate it (the
        # 'PutuBK21@bkubudPutuBK21@bkubud...' bug). If it already holds the right
        # value we leave it; if it can't be set cleanly we DON'T submit.
        if user_el is not None:
            if not _set_login_field(driver, user_el, creds[0]):
                _log("couldn't set the username cleanly (a stale/autofilled value "
                     "wouldn't clear) — NOT submitting, to avoid a concatenated login")
                _clear_fields([user_el, pwd_el])
                return False
            filled.append(user_el)
        if pwd_el is not None:
            if not _set_login_field(driver, pwd_el, creds[1]):
                _log("couldn't set the password cleanly — NOT submitting")
                _clear_fields([user_el, pwd_el])
                return False
            filled.append(pwd_el)
    # else: no stored credentials -> use the browser's remembered values as-is.
    try:
        have_user = bool((user_el.get_attribute("value") or "").strip()) if user_el else False
        have_pwd = bool((pwd_el.get_attribute("value") or "").strip()) if pwd_el else False
    except Exception:  # noqa: BLE001
        have_user = have_pwd = False
    if not have_user or not have_pwd:
        _log("username/password not filled (no credentials.py, and the browser "
             "didn't remember them) — handing to a human")
        _clear_fields(filled)
        return False
    _log("username/password ready ({})".format(
        "typed from credentials.py" if creds else "from the browser's saved login"))

    # Read the CAPTCHA; if not confident, REFRESH it (new image — no submit, so no
    # failed login) and read again, a few times, so we only ever submit a confident
    # read. Refreshing is free; the breaker + cooldown still gate the real submit.
    digits = None
    tries = max(1, int(getattr(config, "CAPTCHA_SOLVE_TRIES", 6)))
    for _attempt in range(tries):
        if _box_has_text():
            _log("a human is typing the CAPTCHA — backing off (no submit)")
            _clear_fields(filled)
            return False
        digits = captcha.solve_captcha(driver, config.CAPTCHA_MIN_LEN, config.CAPTCHA_MAX_LEN)
        if digits:
            break
        if _attempt < tries - 1 and _refresh_captcha(driver):
            continue
        break
    if not digits:
        _log("CAPTCHA not read confidently in {} tr{} — handing to a human".format(
            tries, "y" if tries == 1 else "ies"))
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

    # Final guard: re-assert the username/password right before submitting, in case
    # the browser re-autofilled (and re-concatenated) them after we set them. Abort
    # rather than submit a bad login (which would just burn the breaker + lock risk).
    if creds and not (_set_login_field(driver, user_el, creds[0])
                      and _set_login_field(driver, pwd_el, creds[1])):
        _log("username/password drifted before submit and couldn't be reset — "
             "NOT submitting")
        _clear_fields(filled + [cap_in])
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
        # Offset range relative to today, e.g. {"from_days": -1, "to_days": 1} ->
        # yesterday .. tomorrow (the daily reservation arrivals window).
        if "from_days" in p or "to_days" in p:
            fd = int(p.get("from_days", 0))
            td = int(p.get("to_days", 0))
            return today + datetime.timedelta(days=fd), today + datetime.timedelta(days=td)
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


_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], start=1)}


def _vc_displayed_months(driver):
    """The months (as year*12+month keys) of the panes currently on screen, read
    from the '.vc-title' headers. VHP's range picker shows TWO month panes side by
    side (e.g. 'November 2026' | 'December 2026'), so the displayed month can't be
    inferred from cell counts — the titles name the panes exactly. Returns a sorted
    list (one or two entries), or [] if no title parsed yet (grid still rendering)."""
    out = []
    for t in driver.find_elements(By.CSS_SELECTOR, ".vc-title"):
        try:
            if not t.is_displayed():
                continue
            parts = (t.text or "").strip().split()        # e.g. "November 2026"
            if len(parts) >= 2 and parts[0].lower() in _MONTHS and parts[-1].isdigit():
                out.append(int(parts[-1]) * 12 + _MONTHS[parts[0].lower()])
        except Exception:  # noqa: BLE001
            continue
    return sorted(set(out))


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
    """Click a V-Calendar day by its exact 'id-YYYY-MM-DD' class.

    Pages the calendar until the target date's month is one of the panes on
    screen, then clicks that date's IN-MONTH cell. This is what makes a range
    spanning two months work (e.g. 30/10 -> 05/11): VHP shows two month panes side
    by side, and the same date also appears as a faint spillover cell in the
    neighbouring pane — clicking a spillover cell navigates the grid instead of
    selecting, so the end date would be lost. Requiring ' in-month ' (and excluding
    ' not-in-month ') always picks the real cell; reading the pane titles (not a
    cell count, which two panes make ambiguous) tells us which months are shown."""
    iso = date.strftime("%Y-%m-%d")
    xp_in = ("//*[contains(concat(' ',normalize-space(@class),' '),' id-{iso} ')]"
             "[contains(concat(' ',normalize-space(@class),' '),' in-month ')]"
             "[not(contains(concat(' ',normalize-space(@class),' '),' not-in-month '))]"
             ).format(iso=iso)
    xp_any = "//*[contains(concat(' ',normalize-space(@class),' '),' id-{iso} ')]".format(iso=iso)

    def _click(xp):
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
        return False

    want_ym = date.year * 12 + date.month
    for _ in range(max_nav):
        shown = _vc_displayed_months(driver)          # months of the panes on screen
        if shown:
            if want_ym < shown[0]:
                _vc_click_arrow(driver, "left")        # target is before both panes
            elif want_ym > shown[-1]:
                _vc_click_arrow(driver, "right")       # target is after both panes
            elif _click(xp_in):                        # target month is on screen
                return True
        # else: no title parsed yet (grid still rendering / animating) -> wait
        time.sleep(0.4)
    return _click(xp_in) or _click(xp_any)             # last resort


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
    # VHP's collapse toggle hides the Month/date controls off-screen -> expand first.
    try:
        print("    (period) {}".format(_ensure_filter_panel_open(driver)))
    except Exception:  # noqa: BLE001
        pass
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


def _qfield_by_label(driver, label):
    """The visible .q-field whose label matches `label` (case/space-insensitive,
    exact first then substring), or None."""
    want = " ".join(label.strip().lower().split())
    cands = []
    for f in driver.find_elements(By.CSS_SELECTOR, ".q-field"):
        try:
            if not f.is_displayed():
                continue
            lab = f.find_elements(By.CSS_SELECTOR, ".q-field__label")
            txt = " ".join((lab[0].text or "").strip().lower().split()) if lab else ""
            if txt == want:
                return f
            if want and want in txt:
                cands.append(f)
        except Exception:  # noqa: BLE001 - stale element, keep looking
            continue
    if cands:
        return cands[0]
    # EXTERNAL-label fallback (VHP operational pages, e.g. the room-departure
    # 'To Date'): the label is a heading ABOVE the field, so there's no internal
    # .q-field__label. Find the leaf whose text IS the label, then the q-field that
    # follows it. (' q-field ' as a standalone class matches a real q-field/q-input,
    # not a q-select, which only carries q-field-- modifier classes.)
    up = label.strip().upper()
    xp = ("(//*[not(*) and translate(normalize-space(.),'{lo}','{up}')={w!r}]"
          "/following::*[contains(concat(' ',normalize-space(@class),' '),"
          "' q-field ')])[1]").format(lo=_LOWER, up=_UPPER, w=up)
    for f in driver.find_elements(By.XPATH, xp):
        if _disp_safe(f):
            return f
    return None


def _same_date(shown, date):
    """True if the DD/MM/YY (or DD/MM/YYYY) string `shown` is the given date."""
    s = (shown or "").strip()
    for fmt in ("%d/%m/%y", "%d/%m/%Y"):
        try:
            return datetime.datetime.strptime(s, fmt).date() == date
        except ValueError:
            continue
    return False


def set_single_date(driver, label, date, settle=0.6):
    """Set a SINGLE-date q-field (e.g. the room-departure 'To Date') to `date`
    (datetime.date), shown DD/MM/YY. Tries typing, then a JS value-set, then the
    calendar popup, verifying the field reads back the wanted date each time.
    Prints a diagnostic (field HTML) if it can't. Self-contained; never raises."""
    want = date.strftime(_VHP_DATE_FMT)  # DD/MM/YY
    try:
        field = None
        end = time.time() + 15
        while time.time() < end:
            field = _qfield_by_label(driver, label)
            if field is not None:
                break
            time.sleep(0.5)
        if field is None:
            print("    (to-date) no {!r} field found. Labels: {}".format(
                label, ", ".join(_field_labels(driver)) or "(none)"))
            return False

        def _val():
            try:
                ins = _visible_inputs(field)
                return (ins[0].get_attribute("value") or "").strip() if ins else ""
            except Exception:  # noqa: BLE001
                return ""

        print("    (to-date) want {} | {!r} shows: {}".format(want, label, _val() or "(blank)"))

        # 1) type into an editable input
        for inp in _editable_inputs(field):
            try:
                inp.click()
                time.sleep(0.2)
                inp.send_keys(Keys.CONTROL, "a")
                inp.send_keys(Keys.DELETE)
                inp.send_keys(want)
                time.sleep(0.3)
                inp.send_keys(Keys.ENTER)
                time.sleep(settle)
            except Exception as exc:  # noqa: BLE001
                print("    (to-date) typing failed: {}".format(_describe(exc)))
            _close_overlays(driver)
            if _same_date(_val(), date):
                print("    (to-date) set {} by typing".format(want))
                return True
            break  # only the first editable input

        # NOTE: no raw JS value-set here. On a readonly V-Calendar it only changes
        # the <input> display text (not the component's real date model — a FALSE
        # success) and the events it fires re-render the field (stale element). The
        # calendar click below is the real path, exactly as the range setter does.

        # 3) open the V-Calendar popup (the SAME proven opener the Arrival-Date
        #    range uses — clicks the calendar icon/control, not just the readonly
        #    input) and click the target day. _click_vc_day pages months, so a
        #    To Date in the previous month (e.g. 31/10 while the picker opens on
        #    November) is reached by navigating back.
        popup_dump = ""
        try:
            _popup, popup_dump = _open_and_capture_popup(driver, field)
            if _click_vc_day(driver, date):
                time.sleep(settle)
                _close_overlays(driver)
                # Re-find the field before reading back — the calendar commit can
                # re-render it, so the cached reference may be stale.
                field = _qfield_by_label(driver, label) or field
                if _same_date(_val(), date):
                    print("    (to-date) set {} via calendar".format(want))
                    return True
        except Exception as exc:  # noqa: BLE001
            print("    (to-date) calendar failed: {}".format(_describe(exc)))
        _close_overlays(driver)

        print("    (to-date) WARNING could not set {!r} to {} (shows {}). Field HTML: {}"
              .format(label, want, _val() or "(blank)", _el_html(driver, field)))
        print("    (to-date) popup HTML: {}".format(popup_dump or "(popup did not open)"))
        return False
    except Exception as exc:  # noqa: BLE001 - must never abort the scrape
        print("    (to-date) error: {}".format(_describe(exc)))
        try:
            _close_overlays(driver)
        except Exception:  # noqa: BLE001
            pass
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


def _pace_filter():
    """A short, configurable pause between filter-menu clicks (config.PACING
    'filter_step') so VHP doesn't burst-render the panel — eases the 'Aw, Snap!
    Out of Memory' crash. No-op when the delay is 0/unset."""
    s = config.pace("filter_step", 0.0)
    if s > 0:
        time.sleep(s)


def park_tab(driver, settle=None):
    """Park the tab on about:blank so Chrome frees the heavy report's DOM/memory
    before the next render — the main defence against the 'Aw, Snap! Out of Memory'
    crash when reports run back-to-back. Best-effort; never raises. No-op when
    config.PACING['park_between_reports'] is false."""
    if not (getattr(config, "PACING", {}) or {}).get("park_between_reports", True):
        return
    try:
        driver.get("about:blank")
    except Exception:  # noqa: BLE001 - a parking hiccup must never break the run
        return
    s = settle if settle is not None else config.pace("park_settle", 2.0)
    if s > 0:
        time.sleep(s)


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
    # VHP's collapse toggle hides the whole filter panel off-screen -> expand first,
    # or every dropdown/checkbox/segment click lands out-of-bounds.
    try:
        print("    (config) {}".format(_ensure_filter_panel_open(driver)))
    except Exception:  # noqa: BLE001
        pass
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
        _pace_filter()   # slow filter edits down for memory relief

    for label, want in (report.get("checkboxes") or {}).items():
        try:
            changed = _set_checkbox(driver, label, bool(want))
            print("    (config) checkbox {!r} -> {}{}".format(
                label, "on" if want else "off", "" if changed else " (already)"))
        except Exception as exc:  # noqa: BLE001
            print("    (config) checkbox {!r}: {}".format(label, _describe(exc)))
        _pace_filter()

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
    """Find the single-select Quasar dropdown (q-select) for `label`
    (e.g. 'Reservation Status' / 'Display'). Returns the clickable field or None."""
    want = label.upper()
    # 1) Floating-label controls: the label text is INSIDE the q-select/q-field.
    for cls in ("q-select", "q-field"):
        xp = ("//label[contains(@class,'{cls}')]"
              "[contains(translate(normalize-space(.),'{lo}','{up}'), {w!r})]"
              ).format(cls=cls, lo=_LOWER, up=_UPPER, w=want)
        hits = [e for e in driver.find_elements(By.XPATH, xp) if _disp_safe(e)]
        if hits:
            return _pick_field(hits)
    # 2) EXTERNAL-label controls (the VHP operational pages — reservation grid,
    #    telephone-operator): the label is a separate heading ABOVE the q-select, so
    #    its text isn't inside the control. Find the leaf whose text IS the label,
    #    then the first q-select that follows it in the document.
    leaf = ("//*[not(*) and translate(normalize-space(.),'{lo}','{up}')={w!r}]"
            ).format(lo=_LOWER, up=_UPPER, w=want)
    xp2 = ("({leaf}/following::*[contains(concat(' ',normalize-space(@class),' '),"
           "' q-select ')])[1]").format(leaf=leaf)
    hits = [e for e in driver.find_elements(By.XPATH, xp2) if _disp_safe(e)]
    if hits:
        return hits[0]
    return None


def _dropdown_value(driver, field):
    try:
        return (field.text or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def dropdown_shows(driver, label, want):
    """True if the single-select dropdown `label` currently displays `want`
    (case-insensitive substring of the field's text). Used to GATE a report on a
    required dropdown — e.g. room-departure's entire To-Date model is only valid
    while Display = 'Expected Departure', so we verify it stuck rather than trusting
    the best-effort configure_report."""
    field = _dropdown_field(driver, label)
    if field is None:
        return False
    return want.strip().lower() in _dropdown_value(driver, field).lower()


def _dropdown_labels_diag(driver):
    """Print the dropdown field labels actually on the page — so when a label isn't
    found we can see what to target (same idea as the radio/segment diagnostics)."""
    try:
        info = driver.execute_script(
            "var out=[];"
            "document.querySelectorAll('label.q-select .q-field__label, "
            "label.q-field .q-field__label').forEach(function(e){"
            "var t=(e.textContent||'').trim();if(t)out.push('label:'+t);});"
            "document.querySelectorAll('.q-select').forEach(function(e){"
            "var t=(e.innerText||e.textContent||'').trim().replace(/\\s+/g,' ');"
            "out.push('q-select=\"'+t.slice(0,32)+'\"');});"
            "return out.slice(0,40);")
        print("    (config) dropdowns on page: {}".format(info))
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


def _find_segment_field(driver, wait=10):
    """The CLICKABLE Segment Code control. VHP puts the data-cy on an inner
    q-field__native div that Selenium frequently reports as NOT displayed, which made
    the old finder raise 'not found' and silently skip the de-select (so the export
    came out gross). Return the enclosing .q-field / .q-select label instead — it is
    clickable and reliably 'displayed'. Waits briefly for the filter panel to render;
    returns None only if the control truly isn't on the page."""
    end = time.time() + wait
    while time.time() < end:
        cells = driver.find_elements(By.CSS_SELECTOR, "[data-cy='{}']".format(_SEGMENT_CY))
        if cells:
            cell = cells[0]
            for xp in ("ancestor-or-self::label[contains(concat(' ',normalize-space(@class),' '),' q-field ')][1]",
                       "ancestor-or-self::*[contains(concat(' ',normalize-space(@class),' '),' q-select ')][1]"):
                try:
                    return cell.find_element(By.XPATH, xp)
                except Exception:  # noqa: BLE001 - try the next ancestor pattern
                    continue
            return cell  # last resort: the native div itself
        time.sleep(0.4)
    return None


def _segment_value(driver):
    """The Segment Code field's shown value (e.g. 'ALL' or 'FIT, FIT-OTA, …')."""
    for css in (
        "[data-cy='{}'] .ellipsis".format(_SEGMENT_CY),
        "[data-cy='{}']".format(_SEGMENT_CY),
    ):
        for el in driver.find_elements(By.CSS_SELECTOR, css):
            try:  # textContent works even when Selenium reports the div not-displayed
                txt = (el.text or el.get_attribute("textContent") or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if txt:
                return txt
    return ""


def _real_click(driver, el):
    """A real ActionChains click (scroll into view first); _force_click fallback."""
    from selenium.webdriver.common.action_chains import ActionChains
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        time.sleep(0.15)
    except Exception:  # noqa: BLE001
        pass
    try:
        ActionChains(driver).move_to_element(el).pause(0.05).click().perform()
    except Exception:  # noqa: BLE001
        _force_click(driver, el)


def _segment_item_by_label(driver, label):
    """The open menu's .q-item whose .q-item__label == label (case-insensitive)."""
    want = " ".join((label or "").upper().split())
    for it in driver.find_elements(By.CSS_SELECTOR, ".q-menu .q-item"):
        try:
            labs = it.find_elements(By.CSS_SELECTOR, ".q-item__label")
            if labs and " ".join((labs[0].text or labs[0].get_attribute("textContent")
                                   or "").upper().split()) == want:
                return it
        except Exception:  # noqa: BLE001
            continue
    return None


def _seg_item_checked(item):
    """The aria-checked state (True/False) of a menu item's checkbox, or None."""
    try:
        cb = item.find_element(By.CSS_SELECTOR, "[role='checkbox']")
        return cb.get_attribute("aria-checked") == "true"
    except Exception:  # noqa: BLE001
        return None


def _segment_untick(driver, exclude_labels):
    """Set Segment Code to everything EXCEPT the given labels (e.g. COMPLIMENT,
    HOUSE USE), so the occupancy export is NET.

    VHP's multi-select ignores synthetic clicks and its controls sit below the fold
    on a small window, so ENLARGE the window and use REAL (ActionChains) clicks. The
    open menu is a .q-menu listbox of .q-item rows, each a role=checkbox plus a
    .q-item__label carrying the segment name.
    """
    print("    (config) {}".format(_ensure_filter_panel_open(driver)))  # drawer must be open

    _combo, _owns, opts, log = _open_segment_menu(driver)
    if not opts:
        raise RuntimeError("Segment Code menu would not open ({})".format(" | ".join(log[-3:])))

    # Deterministic start: if a partial selection persisted, re-select All first
    # (a fresh page load already shows ALL).
    if _segment_value(driver).upper() != "ALL":
        allit = _segment_item_by_label(driver, "All")
        if allit is not None:
            _real_click(driver, allit)
            time.sleep(0.6)

    # Match each exclude token by normalized substring against the real labels, so
    # "COMPLIMENT"/"HOUSE USE" hit VHP's exact "COMPLIMENT"/"HOUSE USE" (and any
    # variants). opts came straight from the open menu.
    def _norm(s):
        return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

    tokens = [_norm(x) for x in (exclude_labels or []) if _norm(x)]
    targets = [lbl for lbl in opts
               if lbl.upper() != "ALL" and any(t in _norm(lbl) for t in tokens)]
    if not targets:
        print("    (config) Segment Code: nothing matched {} in {}".format(
            list(exclude_labels), opts[:40]))

    # De-select each target with a REAL click on its .q-item, and verify the
    # checkbox flipped true -> false.
    for lbl in targets:
        item = _segment_item_by_label(driver, lbl)
        if item is None:
            print("    (config) Segment Code: option {!r} not found".format(lbl))
            continue
        before = _seg_item_checked(item)
        _real_click(driver, item)
        time.sleep(0.4)
        _pace_filter()   # ease the render between de-selects
        it2 = _segment_item_by_label(driver, lbl)
        after = _seg_item_checked(it2) if it2 is not None else None
        print("    (config) Segment Code: {!r} checkbox {} -> {}".format(lbl, before, after))

    # Close the dropdown fully so it can't bleed into SEARCH or the export menu.
    for _ in range(3):
        try:
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
        if not any(_disp_safe(m) for m in driver.find_elements(By.CSS_SELECTOR, ".q-menu")):
            break
    print("    (config) Segment Code now shows: {}".format(_segment_value(driver)[:80] or "(blank)"))


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


_MENU_LABELS_JS = (
    "var out=[];document.querySelectorAll('.q-menu .q-item__label').forEach("
    "function(e){var t=(e.textContent||'').trim();if(t)out.push(t);});return out;")


def _collect_menu_labels(driver):
    """Scroll the OPEN q-menu's virtual list top-to-bottom and return its unique
    option labels (first-seen order). The menu must already be open; returns []
    if nothing renders."""
    seen = []
    try:
        driver.execute_script(_SCROLL_MENU_JS, 0, True)  # start at the top
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.2)
    last, stalls = -1.0, 0
    for _ in range(150):
        for lbl in (driver.execute_script(_MENU_LABELS_JS) or []):
            if lbl not in seen:
                seen.append(lbl)
        try:
            pos = float(driver.execute_script(_SCROLL_MENU_JS, 240, False))
        except Exception:  # noqa: BLE001
            break
        if pos <= last:
            stalls += 1
            if stalls >= 3:
                break
        else:
            stalls = 0
        last = pos
        time.sleep(0.12)
    return seen


_SEG_OPTS_JS = r"""
  var owns=arguments[0], out=[], seen={};
  var roots=[].slice.call(document.querySelectorAll('.q-menu'));
  if(owns){var lb=document.getElementById(owns); if(lb)roots.push(lb);}
  roots.forEach(function(root){
    var items=root.querySelectorAll('.q-item,[role=option],li');
    for(var i=0;i<items.length;i++){var t=(items[i].textContent||'').trim();
      if(t&&t.length<40&&!(t in seen)){seen[t]=1;out.push(t);}}
  });
  return out;
"""


def _segment_combo(driver):
    """The focusable role=combobox element inside the Segment Code field."""
    return next((c for c in driver.find_elements(
        By.CSS_SELECTOR, "[data-cy='{}'] [role='combobox']".format(_SEGMENT_CY))), None)


def _maximize(driver):
    """Fit the window to the SCREEN (not an oversized window, which pushes controls
    off-screen -> out-of-bounds). Mirrors maximizing by hand, which works."""
    try:
        info = driver.execute_cdp_cmd("Browser.getWindowForTarget", {})
        if (info.get("bounds") or {}).get("windowState") not in ("maximized", "fullscreen"):
            driver.maximize_window()
            time.sleep(0.4)
    except Exception:  # noqa: BLE001
        try:
            driver.maximize_window()
            time.sleep(0.4)
        except Exception:  # noqa: BLE001
            pass


# --- Filter panel (Quasar drawer) expand/collapse -------------------------
# VHP added a toggle that COLLAPSES the whole left filter panel. When collapsed,
# every filter control (Month picker, Segment Code, even SEARCH) is pushed off
# the viewport, so set_period / segment-untick clicks land out-of-bounds and the
# month picker reads as "not found" — the export then comes out GROSS. The toggle
# is a q-btn with class s-page-drawer__toggle; its chevron icon shows the state:
#   mdi-chevron-double-left  -> drawer OPEN   (clicking it would COLLAPSE)
#   mdi-chevron-double-right -> drawer CLOSED (clicking it EXPANDS)
_DRAWER_TOGGLE_CSS = ".s-page-drawer__toggle"
_DRAWER_STATE_JS = r"""
  var btn=document.querySelector('.s-page-drawer__toggle');
  if(!btn) return 'notoggle';
  var icon=btn.querySelector('i.q-icon');
  var cls=icon?(icon.className||''):'';
  if(cls.indexOf('chevron-double-right')!==-1) return 'closed';
  if(cls.indexOf('chevron-double-left')!==-1)  return 'open';
  return 'unknown';
"""
# Is at least one real filter control actually ON the visible viewport? (A
# collapsed drawer leaves the controls in the DOM but off-screen.) Used to
# confirm an expand worked, and to disambiguate an unreadable chevron.
_CONTROLS_ONSCREEN_JS = r"""
  function onScreen(e){ if(!e) return false; var r=e.getBoundingClientRect();
    if(r.width<2||r.height<2) return false;
    var cx=r.left+r.width/2, cy=r.top+r.height/2;
    return cx>=0 && cy>=0 && cx<=(window.innerWidth||0) && cy<=(window.innerHeight||0); }
  var sel=["[data-cy='field-month']","[data-cy='dropdown-segment-code']"];
  for(var i=0;i<sel.length;i++){ var e=document.querySelector(sel[i]);
    if(e){ var a=e; for(var k=0;k<4 && a;k++){ if(onScreen(a)) return true; a=a.parentElement; } } }
  var btns=document.querySelectorAll('button');
  for(var j=0;j<btns.length;j++){ var t=(btns[j].textContent||'').trim().toUpperCase();
    if(t==='SEARCH' && onScreen(btns[j])) return true; }
  return false;
"""


def _drawer_state(driver):
    try:
        return driver.execute_script(_DRAWER_STATE_JS)
    except Exception:  # noqa: BLE001
        return "unknown"


def _controls_onscreen(driver):
    try:
        return bool(driver.execute_script(_CONTROLS_ONSCREEN_JS))
    except Exception:  # noqa: BLE001
        return False


def _ensure_filter_panel_open(driver, wait=6.0):
    """Make sure the left filter panel (Quasar drawer) is EXPANDED before we drive
    its controls. Maximize first, then — only when the toggle's chevron says the
    drawer is CLOSED (or it's unreadable AND no control is on-screen) — click the
    toggle to expand it and wait until a control is actually on the viewport. Never
    closes an already-open drawer. Returns a short status string (callers print it)."""
    _maximize(driver)
    btns = driver.find_elements(By.CSS_SELECTOR, _DRAWER_TOGGLE_CSS)
    if not btns:
        return "no filter-panel toggle (old VHP layout)"
    btn = btns[0]
    st = _drawer_state(driver)
    if st == "open" or (st == "unknown" and _controls_onscreen(driver)):
        return "filter panel already open (state={})".format(st)
    # 'closed' (or 'unknown' with nothing on-screen) -> expand. The toggle sits at
    # the panel edge and stays on-screen, so a real click reaches it. Try twice.
    for _ in range(2):
        _real_click(driver, btn)
        end = time.time() + wait
        while time.time() < end:
            time.sleep(0.3)
            if _drawer_state(driver) == "open" or _controls_onscreen(driver):
                time.sleep(0.6)  # let the panel's controls finish laying out
                return "expanded filter panel (clicked drawer toggle)"
        btns = driver.find_elements(By.CSS_SELECTOR, _DRAWER_TOGGLE_CSS)
        if btns:
            btn = btns[0]
    return "clicked drawer toggle but still not open (state={}, onscreen={})".format(
        _drawer_state(driver), _controls_onscreen(driver))


def _vp_prepare(driver, el):
    """Maximize the window and scroll `el` to center, so a real click can reach it."""
    _maximize(driver)
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center',inline:'center'});", el)
        time.sleep(0.3)
    except Exception:  # noqa: BLE001
        pass


def _ac_click(driver, el):
    """A REAL mouse click (ActionChains) — opens components that ignore synthetic
    .click(). Makes the element reachable first, and falls back to an offset aim."""
    from selenium.webdriver.common.action_chains import ActionChains
    _vp_prepare(driver, el)
    try:
        ActionChains(driver).move_to_element(el).pause(0.1).click().perform()
    except Exception:  # noqa: BLE001 - center may still be off; aim near top-left
        ActionChains(driver).move_to_element_with_offset(el, 3, 3).pause(0.1).click().perform()


def _pointer_open(driver, el):
    """Dispatch a full pointer+mouse sequence (Quasar v2 opens on pointerdown)."""
    driver.execute_script(r"""
      var e=arguments[0];
      ['pointerover','pointerenter','pointerdown','mousedown','pointerup','mouseup','click'].forEach(function(t){
        try{var C=t.indexOf('pointer')===0?window.PointerEvent:window.MouseEvent;
            e.dispatchEvent(new C(t,{bubbles:true,cancelable:true,view:window,pointerId:1,button:0}));}catch(_){}
      });
    """, el)


def _open_segment_menu(driver):
    """Open the Segment Code dropdown and return (combo, owns, options, log). VHP's
    multi-select ignores synthetic clicks, so try a REAL ActionChains click and the
    full JS mouse-event sequence on the combobox / native div, plus a JS keydown,
    reading options from BOTH .q-menu and the aria-owns listbox."""
    _ensure_filter_panel_open(driver)   # drawer must be expanded or the control is off-screen
    combo = _segment_combo(driver)
    field = _find_segment_field(driver)
    native = next((n for n in driver.find_elements(
        By.CSS_SELECTOR, "[data-cy='{}']".format(_SEGMENT_CY))), None)
    control = None
    if field is not None:
        control = next((c for c in field.find_elements(
            By.CSS_SELECTOR, ".q-field__control")), None)
    owns = (combo.get_attribute("aria-owns") if combo else "") or ""

    def _opts():
        try:
            return driver.execute_script(_SEG_OPTS_JS, owns) or []
        except Exception:  # noqa: BLE001
            return []

    from selenium.webdriver.common.action_chains import ActionChains

    def _focus_key(key):
        # focus the combobox, then send a REAL (trusted) keystroke with no mouse
        # move — sidesteps both the out-of-bounds AND the ignore-synthetic problem.
        driver.execute_script("arguments[0].focus();", combo)
        time.sleep(0.2)
        ActionChains(driver).send_keys(key).perform()

    attempts = []
    if combo is not None:
        attempts.append(("focus+key Enter", lambda: _focus_key(Keys.ENTER)))
        attempts.append(("focus+key ArrowDown", lambda: _focus_key(Keys.ARROW_DOWN)))
        attempts.append(("focus+key Space", lambda: _focus_key(Keys.SPACE)))
    if native is not None:
        attempts.append(("actionchains native", lambda: _ac_click(driver, native)))
        attempts.append(("pointer native", lambda: _pointer_open(driver, native)))
    if control is not None:
        attempts.append(("actionchains control", lambda: _ac_click(driver, control)))
    if combo is not None:
        attempts.append(("actionchains combo", lambda: _ac_click(driver, combo)))
    if field is not None:
        attempts.append(("actionchains field", lambda: _ac_click(driver, field)))

    log = []
    for name, fn in attempts:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            log.append("{}: ERR {}".format(name, _describe(exc)))
            continue
        time.sleep(1.4)
        opts = _opts()
        exp = (combo.get_attribute("aria-expanded") if combo else "?")
        log.append("{}: expanded={} qmenu={} opts={}".format(
            name, exp, len(driver.find_elements(By.CSS_SELECTOR, ".q-menu")), len(opts)))
        if opts:
            return combo, owns, opts, log
    # still nothing — dump the listbox + any popup so we can see where options live
    try:
        dump = driver.execute_script(r"""
          var owns=arguments[0], out=[]; var lb=owns?document.getElementById(owns):null;
          out.push('listbox present='+(!!lb)+(lb?(' html='+(lb.outerHTML||'').replace(/\s+/g,' ').slice(0,700)):''));
          var pops=document.querySelectorAll('.q-menu,[role=listbox],.q-virtual-scroll,.q-dialog');
          out.push('popup-ish='+pops.length);
          return out.join(' || ');
        """, owns)
        log.append("DUMP: " + (dump or ""))
    except Exception as exc:  # noqa: BLE001
        log.append("dump ERR " + _describe(exc))
    return combo, owns, [], log


def dump_segment_options(driver):
    """Open the Segment Code multi-select and return its option labels. Probes
    several open methods (VHP's dropdown is non-standard) and reads options from the
    .q-menu AND the aria-owns listbox. Never raises; returns a list of strings."""
    try:
        _combo, _owns, opts, log = _open_segment_menu(driver)
        _close_overlays(driver)
        if opts:
            return opts
        return ["(could not open/enumerate the Segment menu — probe log:)"] + log
    except Exception as exc:  # noqa: BLE001
        return ["(error: {})".format(_describe(exc))]


# --- Segment de-select DIAGNOSTIC ----------------------------------------
# The UI shows the segments deselected (checkbox True->False, field shows the net
# list) but the exported "Room Occupied" stays GROSS. To see whether the filter
# actually reaches the backend, record every request's URL + body (works on an
# attached session, no CDP events needed) so we can read the SEARCH request.
_REQ_RECORDER_JS = r"""
  if (window.__vhpRecOn) return 'already-installed';
  window.__vhpRecOn = true;
  window.__vhpReqs = [];
  try {
    var of = window.fetch;
    if (of) window.fetch = function(){
      try { var u=arguments[0], o=arguments[1]||{};
        window.__vhpReqs.push({t:'fetch', m:(o.method||'GET'),
          url:((u&&u.url)||u||'')+'', body:(o.body!=null?(''+o.body):null)}); } catch(e){}
      return of.apply(this, arguments);
    };
  } catch(e){}
  try {
    var XP = window.XMLHttpRequest && window.XMLHttpRequest.prototype;
    if (XP) {
      var oo = XP.open, os = XP.send;
      XP.open = function(m,u){ try{ this.__m=m; this.__u=u; }catch(e){} return oo.apply(this, arguments); };
      XP.send = function(b){ try{ window.__vhpReqs.push({t:'xhr', m:(this.__m||''),
          url:(this.__u||'')+'', body:(b!=null?(''+b):null)}); }catch(e){}
        return os.apply(this, arguments); };
    }
  } catch(e){}
  return 'installed';
"""


def install_request_recorder(driver):
    """Patch window.fetch + XMLHttpRequest so every request's URL+body lands in
    window.__vhpReqs. Call once BEFORE pressing SEARCH; survives the SPA's own XHRs
    (axios uses XMLHttpRequest, so the prototype patch catches it)."""
    try:
        return driver.execute_script(_REQ_RECORDER_JS)
    except Exception as exc:  # noqa: BLE001
        return "recorder ERR {}".format(_describe(exc))


def clear_recorded_requests(driver):
    """Empty the recorder buffer (so a later dump shows only new requests)."""
    try:
        driver.execute_script("if(window.__vhpReqs)window.__vhpReqs.length=0;return 1;")
    except Exception:  # noqa: BLE001
        pass


def dump_recorded_requests(driver, needles=("report", "occupancy", "vhp-node6", "rest",
                                            "forecast", "vhpss", "prepare", "segment", "search")):
    """Recorded requests whose URL OR body mentions a needle (case-insensitive),
    as 'METHOD url | body='. If none match, fall back to the last 10 captured."""
    try:
        reqs = driver.execute_script("return window.__vhpReqs || [];") or []
    except Exception as exc:  # noqa: BLE001
        return ["(read ERR {})".format(_describe(exc))]
    nd = [n.lower() for n in needles]

    def _fmt(r):
        u = (r.get("url") or "")
        b = (r.get("body") or "")
        return "{} {} | body={}".format((r.get("m") or r.get("t") or "").upper(),
                                        u[:500], (b[:1500] if b else "(none)"))

    hits = [r for r in reqs
            if any(n in ((r.get("url") or "") + " " + (r.get("body") or "")).lower() for n in nd)]
    if hits:
        return [_fmt(r) for r in hits]
    return ["(no request matched {}; last {} of {} captured:)".format(
        list(needles), min(10, len(reqs)), len(reqs))] + [_fmt(r) for r in reqs[-10:]]


def segment_checked_map(driver):
    """Reopen the Segment menu read-only and return {label: checked_bool} for every
    option, then close. Use to see whether COMPLIMENT/HOUSE USE stay deselected after
    SEARCH (i.e. whether SEARCH reset the model)."""
    _open_segment_menu(driver)
    out = {}
    for it in driver.find_elements(By.CSS_SELECTOR, ".q-menu .q-item"):
        try:
            labs = it.find_elements(By.CSS_SELECTOR, ".q-item__label")
            if not labs:
                continue
            name = " ".join((labs[0].text or labs[0].get_attribute("textContent") or "").split())
            if name:
                out[name] = _seg_item_checked(it)
        except Exception:  # noqa: BLE001
            continue
    for _ in range(3):
        try:
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.2)
        if not any(_disp_safe(m) for m in driver.find_elements(By.CSS_SELECTOR, ".q-menu")):
            break
    return out


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
    """Find + click SEARCH, retrying on a transient/stale element — setting a date
    or dropdown can re-render the toolbar between finding the button and clicking
    it (StaleElementReferenceException), so re-find and retry."""
    last = None
    for _ in range(4):
        try:
            btn = WebDriverWait(driver, 30).until(
                EC.element_to_be_clickable((By.XPATH, SEARCH_BTN)))
            _safe_click(driver, btn)
            return
        except WebDriverException as exc:  # includes StaleElementReferenceException
            last = exc
            time.sleep(0.5)
    if last is not None:
        raise last


def _count_cells_with_text(driver, need=3):
    """Count Quasar table cells with non-empty text, tolerating cells that go STALE
    while the grid is re-rendering (reading .text on a detached cell raises). Stops
    once `need` is reached."""
    n = 0
    for c in driver.find_elements(By.CSS_SELECTOR, "table.q-table td"):
        try:
            if (c.text or "").strip():
                n += 1
                if n >= need:
                    return n
        except WebDriverException:  # cell detached mid-re-render -> skip it
            continue
    return n


def wait_for_report_data(driver, timeout=60):
    """Wait until the Quasar table has real cells (data loaded via XHR)."""
    end = time.time() + timeout
    while time.time() < end:
        if _count_cells_with_text(driver) >= 3:
            return
        time.sleep(1)
    raise TimeoutError("report data did not load after SEARCH")


def wait_for_report_ready(driver, timeout=60, settle=4.0, min_wait=6.0):
    """Like wait_for_report_data but distinguishes a LEGITIMATELY EMPTY grid from a
    page that never loaded — returns 'data' if real cells appeared, or 'empty' once
    the loading spinner has cleared and the grid has stayed empty for `settle`
    seconds (after a `min_wait` floor so slow XHR isn't called empty too early).
    Raises TimeoutError only if neither happens (a genuine load failure).

    Needed by the room-departure flow: the Expected-Departure view at To Date = D-1
    can correctly be empty (zero in-house guests), which must report rooms=0, not
    abort all three dates."""
    start = time.time()
    end = start + timeout
    calm_since = None
    spin_sel = ".q-inner-loading, .q-spinner, .q-loading-bar, .q-loading"
    while time.time() < end:
        if _count_cells_with_text(driver) >= 3:
            return "data"
        spinning = any(_disp_safe(e) for e in driver.find_elements(By.CSS_SELECTOR, spin_sel))
        now = time.time()
        if spinning:
            calm_since = None
        else:
            if calm_since is None:
                calm_since = now
            if now - start >= min_wait and now - calm_since >= settle:
                return "empty"
        time.sleep(0.5)
    raise TimeoutError("report grid did not settle (no data and no empty-state)")


# A cheap signature of the grid's content so we can tell when it STOPS changing
# after a filtered SEARCH: the cell count + the head and TAIL of all cell text
# (the summary rows — Room Occupied / Occupancy % — live at the end and are exactly
# what flips gross<->net). Changes while the filtered re-render is still in flight.
_REPORT_SIG_JS = r"""
  var cells = document.querySelectorAll('table.q-table td');
  var parts = [String(cells.length)];
  for (var i=0;i<cells.length;i++){ var t=(cells[i].textContent||'').trim(); if(t) parts.push(t); }
  var s = parts.join('|');
  return s.length + '#' + s.slice(0,300) + '#' + s.slice(-300);
"""


def _report_signature(driver):
    try:
        return driver.execute_script(_REPORT_SIG_JS)
    except Exception:  # noqa: BLE001 - a read hiccup just looks like 'changed'
        return None


def wait_for_report_stable(driver, timeout=45, min_wait=None, settle=None, baseline=None):
    """After SEARCH, wait until the grid HAS data AND its content has STOPPED
    changing for `settle` seconds, so a filtered re-render (e.g. gross -> net) has
    fully replaced the previous result before we export. This is the fix for the
    'screen shows 341 but the CSV says 354' bug: wait_for_report_data returns on the
    stale cells still on screen, so we export too early. Never raises — on timeout it
    returns and lets the export proceed (data is present, just slow to settle).

    baseline: the grid signature captured JUST BEFORE SEARCH. When given, we first
    wait for the grid to CHANGE from it — i.e. the filtered search actually took
    effect — so we never measure stability on the stale pre-search grid and return
    gross while the net XHR is still in flight. If it never changes within `min_wait`
    (the filtered result genuinely equals the previous one), we accept and go on.

    min_wait / settle default to config.PACING 'after_search' / 'data_stable'."""
    min_wait = config.pace("after_search", 3.0) if min_wait is None else min_wait
    settle = config.pace("data_stable", 3.0) if settle is None else settle
    start = time.time()
    end = start + timeout
    changed = baseline is None   # with no baseline, treat as 'already changed'
    last_sig, stable_since = None, None
    while time.time() < end:
        now = time.time()
        if _count_cells_with_text(driver) < 3:
            # Data is normally already present (the caller waited). Don't hang if the
            # grid is momentarily empty mid-re-render; give up the stability wait
            # after a short grace and let the export proceed/fail naturally.
            if now - start >= min_wait + 5:
                return
            last_sig, stable_since = None, None
            time.sleep(0.5)
            continue
        sig = _report_signature(driver)
        if not changed:
            # Hold until the filtered search has visibly re-rendered the grid.
            if sig is not None and sig != baseline:
                changed = True
            elif now - start >= min_wait:
                changed = True   # net result == previous one -> nothing to wait for
            else:
                last_sig = sig
                time.sleep(0.5)
                continue
        if sig is not None and sig == last_sig:
            if stable_since is None:
                stable_since = now
            if now - start >= min_wait and now - stable_since >= settle:
                return   # changed, present, AND unchanged long enough -> safe to export
        else:
            stable_since = None   # still changing -> reset the calm timer
        last_sig = sig
        time.sleep(0.5)
    return


# A compact snapshot of the telephone-operator (departure) page for the LOG, so a
# failed BKDU departure pull is diagnosable without watching the office PC: the
# Display value, any open-menu options, table headers, row count, spinner, crash
# flag, URL and whether a login form is showing.
_TELOP_STATE_JS = r"""
  function txt(e){ return e ? (e.textContent||'').replace(/\s+/g,' ').trim() : ''; }
  var out = {};
  try { out.url = location.href; } catch(e){ out.url = ''; }
  var body = (document.body && document.body.innerText || '');
  out.crash = /aw,\s*snap|out of memory|err_out_of_memory/i.test(body.slice(0,800));
  var disp = document.querySelector("[data-cy='dropdown-display']");
  out.display = disp ? txt(disp).slice(0,60) : '(no dropdown-display)';
  var ths = document.querySelectorAll('table.q-table th');
  out.headers = [].map.call(ths, txt).filter(Boolean).slice(0,24);
  var opts = document.querySelectorAll('.q-menu .q-item__label');
  out.menu = [].map.call(opts, txt).filter(Boolean).slice(0,24);
  try { out.rows = document.querySelectorAll('table.q-table tbody tr').length; } catch(e){ out.rows = -1; }
  var spin = document.querySelectorAll('.q-inner-loading,.q-spinner,.q-loading-bar,.q-loading');
  out.spinning = [].some.call(spin, function(e){ var r=e.getBoundingClientRect(); return r.width>0 && r.height>0; });
  var pw = document.querySelector("input[type='password']");
  out.loginVisible = !!(pw && pw.offsetParent);
  return JSON.stringify(out);
"""


def telop_page_state(driver):
    """One-line page-state snapshot for the log (see _TELOP_STATE_JS). Never raises."""
    try:
        return driver.execute_script(_TELOP_STATE_JS)
    except Exception as exc:  # noqa: BLE001
        return "(page-state read failed: {})".format(_describe(exc))


def dump_display_options(driver):
    """Open the 'Display' dropdown and return its option labels (so a '[display] not
    active' failure shows what views VHP actually offers for this property). Best-
    effort: returns a list of strings, or a short note. Leaves the menu closed."""
    try:
        fld = _dropdown_field(driver, "Display")
        if fld is None:
            return ["(no 'Display' dropdown found)"]
        _real_click(driver, fld)
        time.sleep(0.6)
        labels = [(e.text or e.get_attribute("textContent") or "").strip()
                  for e in driver.find_elements(By.CSS_SELECTOR, ".q-menu .q-item__label")]
        labels = [s for s in labels if s]
        for _ in range(3):
            try:
                driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.2)
            if not any(_disp_safe(m) for m in driver.find_elements(By.CSS_SELECTOR, ".q-menu")):
                break
        return labels or ["(Display menu opened but no options read)"]
    except Exception as exc:  # noqa: BLE001
        return ["(could not read Display options: {})".format(_describe(exc))]


# Count DISTINCT room numbers in the telephone-operator grid, so we can tell a
# genuinely EMPTY departure day (no rooms) from a grid that has data. VHP's
# "Export to CSV" produces NO download file for an empty grid, so exporting it
# hangs for the full 120s and fails the feed — we skip the export when this is 0.
_DEPARTURE_ROOMS_JS = r"""
  var tbl = document.querySelector('table.q-table');
  if (!tbl) return -1;
  var ths = tbl.querySelectorAll('th'), idx = -1;
  for (var i=0;i<ths.length;i++){ if(/room\s*number/i.test(ths[i].textContent||'')){ idx=i; break; } }
  if (idx < 0) return -1;                       // layout we don't recognise -> don't assume empty
  var rooms = {}, rows = tbl.querySelectorAll('tbody tr');
  for (var r=0;r<rows.length;r++){
    var tds = rows[r].querySelectorAll('td');
    if (idx < tds.length){
      var v = (tds[idx].textContent||'').trim();
      if (v && /\d/.test(v)) rooms[v] = 1;      // a real room number contains a digit
    }
  }
  return Object.keys(rooms).length;
"""


def departure_room_count(driver):
    """Distinct room numbers currently in the telephone-operator grid. 0 = an empty
    departure day (nothing to export); -1 = the grid/column wasn't found (so DON'T
    assume empty — fall through to the normal export). Never raises."""
    try:
        n = driver.execute_script(_DEPARTURE_ROOMS_JS)
        return int(n) if n is not None else -1
    except Exception:  # noqa: BLE001
        return -1


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
    # Opt-in gate: for a report whose data depends on a dropdown (e.g. the daily
    # reservation feed needs 'Reservation Status' = 'Expected Arrival' or it misses
    # already-arrived guests), VERIFY each named dropdown actually stuck and SKIP
    # rather than export the wrong view. Default off, so other reports are unchanged.
    if report.get("require_dropdowns"):
        for lbl, val in (report.get("dropdowns") or {}).items():
            if not dropdown_shows(driver, lbl, val):
                raise RuntimeError(
                    "[dropdown] required {!r}={!r} not active — skipping report to "
                    "avoid exporting the wrong view".format(lbl, val))
    pre_sig = _report_signature(driver)   # grid signature BEFORE the filtered search
    step("click SEARCH", lambda: click_search(driver))
    # A heavy report (e.g. a 10-month guest list) can take minutes for VHP to return
    # its data; a report may raise its own waits via "search_timeout"/"download_timeout".
    search_to = int(report.get("search_timeout") or 60)
    step("wait for data", lambda: wait_for_report_data(driver, timeout=search_to))
    return export_and_download(driver, prop, report["name"], download_dir,
                               step=step, baseline=pre_sig,
                               download_timeout=report.get("download_timeout"))


def export_and_download(driver, prop, report_name, download_dir, step=None, baseline=None,
                        download_timeout=None):
    """Settle the grid, (re)assert the download dir, press Print -> CSV, wait for the
    file, and return its renamed path. Shared by scrape_report and the daily-JSON
    departure flow (which re-exports the same page once per target date). `step`, if
    given, wraps each sub-step so failures read like '[export Print CSV] ...'.
    `baseline` is the grid signature from BEFORE SEARCH (so we wait for the filtered
    re-render to land before exporting). `download_timeout` overrides how long we wait
    for the file (None -> config.DOWNLOAD_TIMEOUT) — raise it for a large export."""
    def _run(name, fn):
        return step(name, fn) if step else fn()

    # Wait until the grid has STOPPED changing before exporting. After a filtered
    # SEARCH the previous (e.g. gross) result stays on screen until the filtered XHR
    # replaces it NET, so exporting too early captured the stale grid (the screen
    # then showed the right numbers but the CSV was wrong). This + the settle below
    # make sure the on-screen data is final before Print -> CSV.
    _run("wait for stable data", lambda: wait_for_report_stable(driver, baseline=baseline))
    s = config.pace("before_export", 1.0)
    if s > 0:
        time.sleep(s)
    # Re-assert the download directory (page navigation can reset it).
    for _cmd in ("Browser.setDownloadBehavior", "Page.setDownloadBehavior"):
        try:
            driver.execute_cdp_cmd(_cmd, {"behavior": "allow", "downloadPath": download_dir})
        except Exception:  # noqa: BLE001
            pass
    before = snapshot_dirs(config.DOWNLOAD_WATCH)  # snapshot BEFORE the click
    since = time.time()                            # mtime floor for the download
    _run("export Print CSV", lambda: export_print_csv(driver))
    raw = _run("wait for download",
               lambda: wait_for_download(config.DOWNLOAD_WATCH, before=before, since=since,
                                         timeout=download_timeout))
    return rename_download(raw, prop["code"], report_name, download_dir)
