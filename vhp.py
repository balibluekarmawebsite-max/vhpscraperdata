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
import re
import time

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


def _has_captcha(driver):
    """True if the login screen is showing a CAPTCHA (which blocks auto-login)."""
    if driver.find_elements(
        By.XPATH,
        "//input[contains(translate(@placeholder,"
        "'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'CAPTCHA')]",
    ):
        return True
    for el in driver.find_elements(By.XPATH, "//*[contains({}, 'CAPTCHA')]".format(_ci("."))):
        try:
            if el.is_displayed():
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def ensure_logged_in(driver, prop, creds, probe_url):
    """Ensure we're logged in, reusing a saved session when possible.

    1. Navigate to probe_url (a real authenticated page). If it renders with no
       login form, the saved session is still valid -> done (no login needed).
    2. Otherwise a login form is showing:
       * If it has a CAPTCHA, we cannot auto-login -> LoginRequired. The human
         runs login.py once, solves the CAPTCHA, and the session is reused.
       * Else fill the username/password and submit.
    """
    driver.get(probe_url)
    time.sleep(3)  # let the SPA render (app or login form)

    if _login_form(driver) is None:
        return  # saved session still valid -> logged in

    if _has_captcha(driver):
        raise LoginRequired(
            "{code}: the VHP window is not logged in — log into VHP by hand in the "
            "'{code}' Chrome window (open it with open-{code}.bat), then re-run.".format(
                code=prop["code"]
            )
        )

    form = _login_form(driver)
    if not creds or form is None or form[0] is None:
        raise LoginRequired(
            "{code}: the VHP window is not logged in — log into VHP by hand in the "
            "'{code}' Chrome window, then re-run.".format(code=prop["code"])
        )

    user_el, pwd_el, submit_el = form
    user_el.clear()
    user_el.send_keys(creds[0])
    pwd_el.clear()
    pwd_el.send_keys(creds[1])
    if submit_el is not None:
        _safe_click(driver, submit_el)
    else:
        pwd_el.send_keys(Keys.RETURN)

    end = time.time() + 40
    while time.time() < end:
        if _login_form(driver) is None:
            return
        time.sleep(0.5)
    raise LoginRequired(
        "{}: login did not complete — check the username/password".format(prop["code"])
    )


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


def _period_range(report):
    """Return (start_date, end_date) datetime.date for report['period'], or None
    to leave VHP's default untouched.

        'last_7_days'   -> the 7 days ending today        (today-6 .. today)
        'current_month' -> 1st of this month .. today
        {'days_back': N, 'include_today': bool}  -> custom trailing window
        None / anything else -> None
    """
    today = datetime.date.today()
    p = report.get("period")
    if isinstance(p, dict):
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


def _try_calendar_range(driver, field, start, end):
    """Drive the QDate popup to select start..end (QDate range mode = two day
    clicks). Dumps the popup HTML once so a miss is fixable in one round-trip."""
    popup, dump = _open_and_capture_popup(driver, field)
    print("    (period) popup opened on click: {}".format(dump))
    if popup is None:
        _close_overlays(driver)
        return False
    for label, d in (("start", start), ("end", end)):
        _qdate_goto(driver, d.year, d.month)
        if not _click_qday(driver, d.day):
            print("    (period) could not click {} day {}".format(label, d.day))
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


def configure_report(driver, report):
    """Best-effort: apply the report's filter settings before SEARCH.

    Optional report fields in config.py:
        checkboxes:      {"Show Revenue": True}      -> set each checkbox
        segment_untick:  ["Complimentary", "House Use"]  -> de-select in Segment Code

    Never raises — it prints what it did (or couldn't) and the scrape carries on.
    """
    if not (report.get("checkboxes") or report.get("segment_untick")):
        return
    # Wait for the left filter panel to render (it can lag behind SEARCH).
    end = time.time() + 20
    while time.time() < end:
        if driver.find_elements(By.CSS_SELECTOR, "[data-cy='{}']".format(_SEGMENT_CY)) or \
                driver.find_elements(By.XPATH, "//*[contains(@class,'q-checkbox')]"):
            break
        time.sleep(0.5)

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

    _safe_click(driver, field)  # open the dropdown
    time.sleep(1.0)

    # Deterministic start: make sure everything is selected.
    if _segment_value(driver).upper() != "ALL":
        allopt = _find_option(driver, "All")
        if allopt is not None:
            _safe_click(driver, allopt)
            time.sleep(0.7)

    for lbl in exclude_labels:
        item = _find_option(driver, lbl)
        if item is None:
            print("    (config) Segment Code: {!r} not found".format(lbl))
            continue
        _safe_click(driver, item)
        print("    (config) Segment Code: de-selected {!r}".format(lbl))
        time.sleep(0.4)

    try:  # close the dropdown so it doesn't block SEARCH
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.4)
    print("    (config) Segment Code now shows: {}".format(_segment_value(driver)[:70] or "(blank)"))


def _find_option(driver, label):
    """Find a dropdown option whose q-item label EXACTLY equals `label`,
    scrolling the virtually-scrolled menu until it renders. Returns the
    clickable q-item row, or None."""
    want = label.upper()
    xp = (
        "//div[contains(@class,'q-item')]"
        "[.//div[contains(@class,'q-item__label')]"
        "[translate(normalize-space(.),'{lo}','{up}')={w!r}]]"
    ).format(lo=_LOWER, up=_UPPER, w=want)
    last = -1.0
    for _ in range(40):
        for el in driver.find_elements(By.XPATH, xp):
            if el.is_displayed():
                return el
        moved = driver.execute_script(
            "var m=document.querySelector('.q-menu .scroll, .q-menu .q-virtual-scroll,"
            " .q-virtual-scroll, .q-menu'); if(!m){return -1;}"
            " var b=m.scrollTop; m.scrollTop=b+200; return m.scrollTop;"
        )
        try:
            moved = float(moved)
        except Exception:  # noqa: BLE001
            moved = -1.0
        time.sleep(0.25)
        if moved == last:  # scroll didn't advance -> reached the end
            break
        last = moved
    return None


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
    for btn in reversed(buttons):  # printer is the right-most icon; try it first
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        except Exception:  # noqa: BLE001
            pass
        try:
            btn.click()
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
        os.makedirs(f, exist_ok=True)
    return folders


def snapshot_dirs(folders=None):
    """Record each folder's current files, to call BEFORE triggering a download
    so even an instant download is detected as new."""
    folders = _norm_folders(folders)
    return {f: set(os.listdir(f)) for f in folders}


def wait_for_download(folders=None, timeout=None, before=None):
    """Return the path of a newly finished download, watching one or more
    folders (Chrome writes a temporary .crdownload until it finishes). Pass
    `before` from snapshot_dirs() taken before the download was triggered."""
    if timeout is None:
        timeout = config.DOWNLOAD_TIMEOUT
    folders = _norm_folders(folders)
    if before is None:
        before = {f: set(os.listdir(f)) for f in folders}
    end = time.time() + timeout
    while time.time() < end:
        for f in folders:
            new_files = set(os.listdir(f)) - before[f]
            done = [
                x for x in new_files
                if not x.endswith(".crdownload") and not x.endswith(".tmp")
            ]
            if done:
                done.sort(key=lambda x: os.path.getmtime(os.path.join(f, x)), reverse=True)
                return os.path.join(f, done[0])
        time.sleep(1)
    raise TimeoutError(
        "download did not finish within {}s (watched: {})".format(timeout, ", ".join(folders))
    )


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
    step("export Print CSV", lambda: export_print_csv(driver))
    raw = step("wait for download",
               lambda: wait_for_download(config.DOWNLOAD_WATCH, before=_before))
    return rename_download(raw, prop["code"], report["name"], download_dir)
