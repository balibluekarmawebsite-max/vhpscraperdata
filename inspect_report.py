"""
inspect_report.py — one-off helper to SEE a report's filter/date controls.

Read-only: it attaches to a property's Chrome window, opens a report, and prints
the filter fields (labels, data-cy, values) + any date control it finds. It does
NOT press SEARCH, export, or change anything. We use it to work out how to drive
a control we haven't wired yet — e.g. picking a specific month on the
yearly-forecast report (so "section B for a past month" can be enabled).

    python inspect_report.py            # inspects section B (yearly-forecast) on BKDS
    python inspect_report.py C          # inspects section C (reservation) on BKDS
    python inspect_report.py B BKDU     # section B on BKDU

Send me everything it prints.
"""
import sys
import time

import config
import vhp
from browser import kill_stale_chromedrivers, make_driver_attached
from runlock import RunLock

# JS: list every visible filter control (q-field / data-cy / <select>) with its
# label, data-cy, current value, readonly flag and a short outerHTML — so a
# month/year/period picker is identifiable even if it's not a "Date" field.
_DUMP_CONTROLS_JS = r"""
var out = [];
var seen = [];
function add(e, kind){
  var r = e.getBoundingClientRect();
  if (r.width < 1 || r.height < 1) return;           // skip hidden
  if (seen.indexOf(e) !== -1) return; seen.push(e);
  var cy = e.getAttribute('data-cy') || '';
  var l = e.querySelector ? e.querySelector('.q-field__label') : null;
  var label = l ? (l.textContent||'').trim() : '';
  var inp = e.querySelector ? e.querySelector('input, select, textarea') : null;
  var val = inp ? (inp.value || '') : '';
  var ro  = inp ? (inp.readOnly ? 'readonly' : 'editable') : '';
  var ph  = inp ? (inp.placeholder || '') : '';
  var html = (e.outerHTML || '').replace(/\s+/g,' ').slice(0, 300);
  out.push(kind + ' | cy=' + JSON.stringify(cy) + ' label=' + JSON.stringify(label)
           + ' val=' + JSON.stringify(val) + ' ' + ro + ' ph=' + JSON.stringify(ph)
           + '\n      html=' + html);
}
document.querySelectorAll('.q-field').forEach(function(e){ add(e,'q-field'); });
document.querySelectorAll('[data-cy]').forEach(function(e){ add(e,'data-cy'); });
document.querySelectorAll('select').forEach(function(e){ add(e,'select'); });
// anything small whose text is just a year or a month name (a year/month picker display)
var re = /^(20\d\d|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|January|February|March|April|May|June|July|August|September|October|November|December)\b/i;
document.querySelectorAll('body *').forEach(function(e){
  if (e.children.length > 1) return;
  var t = (e.textContent||'').trim();
  if (t && t.length < 16 && re.test(t)) add(e, 'maybe-picker');
});
return out.slice(0, 60).join('\n');
"""


def main(argv):
    section = (argv[0] if len(argv) > 0 else "B").upper()
    code = (argv[1] if len(argv) > 1 else "bkds").lower()

    report = config.report_by_section(section)
    if report is None:
        print("no report for section {!r} (use B or C)".format(section))
        return 2
    prop = None
    for p in config.PROPERTIES:
        if p["code"].lower() == code:
            prop = p
            break
    if prop is None:
        print("no property {!r}".format(code))
        return 2

    lock = RunLock(config.RUN_LOCK_PATH)
    if not lock.acquire(wait=120):
        print("another VHP run is using Chrome right now — try again in a minute")
        return 1
    try:
        kill_stale_chromedrivers()
        print("attaching to {} (port {}) and opening: {}".format(
            prop["code"], prop["debug_port"], report["url"]))
        driver = make_driver_attached(prop["debug_port"], config.DOWNLOADS_DIR)
        vhp.ensure_logged_in(driver, prop, None, report["url"])
        vhp.open_report(driver, report)
        time.sleep(2.5)  # let the filter panel hydrate

        print("\n===== FIELD LABELS =====")
        print(", ".join(vhp._field_labels(driver)) or "(none)")

        print("\n===== DATE FIELD (reservation-style heuristic) =====")
        f = vhp._find_date_field(driver)
        if f is not None:
            print("FOUND. current value: {!r}".format(vhp._date_field_value(driver, f)))
            print("HTML:\n" + vhp._el_html(driver, f, 2500))
        else:
            print("no 'Date' field matched — this report uses a different control.")

        print("\n===== PAGE DIAG (all inputs + date-looking text) =====")
        print(vhp._dump_date_diag(driver))

        print("\n===== ALL FILTER CONTROLS (q-field / data-cy / select / pickers) =====")
        try:
            print(driver.execute_script(_DUMP_CONTROLS_JS))
        except Exception as exc:  # noqa: BLE001
            print("controls dump failed: {}".format(exc))
    finally:
        lock.release()
    print("\n----- done (nothing was changed). Send me everything above. -----")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
