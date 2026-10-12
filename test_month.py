"""
test_month.py — set the yearly-forecast month picker to a given month, export the
CSV, and SAVE IT LOCALLY. It does NOT upload anything. Use it to confirm the data
is correct (e.g. that setting 01/2026 really gives Jan 2026 -> Dec 2026 actuals)
before we wire "Section B for a past month" into the dashboard upload path.

    python test_month.py 2026-01          # BKDS, start month = January 2026
    python test_month.py 2026-01 bkdu     # BKDU

The saved file is named ...-start-<month>-... so it's easy to tell apart from the
daily run's file. Open it and send it to me.
"""
import datetime
import sys

import config
import vhp
from browser import kill_stale_chromedrivers, make_driver_attached
from runlock import RunLock


def main(argv):
    month = argv[0] if argv else datetime.date.today().strftime("%Y-%m")
    code = (argv[1] if len(argv) > 1 else "bkds").lower()

    prop = next((p for p in config.PROPERTIES if p["code"] == code), None)
    if prop is None:
        print("no property {!r} (use bkds/bkdu/bkv)".format(code))
        return 2

    base = config.report_by_section("B")
    report = dict(base)                                   # keep Show Revenue + segment untick + url
    report["name"] = "{}-start-{}".format(base["name"], month)  # distinct filename
    report["period"] = {"month": month}
    report["period_required"] = True                      # fail loudly if the month can't be set

    lock = RunLock(config.RUN_LOCK_PATH)
    if not lock.acquire(wait=120):
        print("another VHP run is using Chrome right now — try again in a minute")
        return 1
    try:
        kill_stale_chromedrivers()
        print("attaching to {} (port {}); setting yearly-forecast start month = {}".format(
            prop["code"], prop["debug_port"], month))
        driver = make_driver_attached(prop["debug_port"], config.DOWNLOADS_DIR)
        vhp.ensure_logged_in(driver, prop, None, report["url"])
        path = vhp.scrape_report(driver, prop, report, config.DOWNLOADS_DIR)
    finally:
        lock.release()

    print("\n===== SAVED (NOT uploaded) =====")
    print(path)
    print("Open that CSV and send it to me — I'll confirm the columns are {} -> "
          "+11 months with the right actuals.".format(month))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
