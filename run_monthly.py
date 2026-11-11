"""
run_monthly.py — pull the MONTHLY sales feeds and POST them to the dashboard:
    * Account Production (YTD)  -> /api/sales/account-production  (Sales Highlight)
    * Room Types (actuals)      -> /api/sales/room-types         (Rooms Analytics)

    python run_monthly.py                       # prev month, all props, all feeds, DRY-RUN
    python run_monthly.py bkds                  # prev month, BKDS, all feeds, dry-run
    python run_monthly.py all --month 2026-06   # June, all props, all feeds, dry-run
    python run_monthly.py all --month 2026-06 --post         # ... POST for real
    python run_monthly.py bkds --feed forecast-room-by-room-type --post   # one feed only

Default month = the PREVIOUS completed month (run on 1 Dec -> reports Nov). Default
is DRY-RUN (prints the JSON, posts nothing); add --post to POST. Each feed sets its
own Date range (account-production = YTD Jan 1..month-end; room-types = the report
month itself). Takes the shared run-lock; attaches to the open VHP windows (log in
first). This is what run-monthly.bat runs on the 1st of each month.
"""
import datetime
import sys
import time

import config
import daily_reports
import vhp
from browser import kill_stale_chromedrivers, make_driver_attached
from runlock import RunLock


def _prev_month(today=None):
    """'YYYY-MM' of the previous completed month."""
    first = (today or datetime.date.today()).replace(day=1)
    prev = first - datetime.timedelta(days=1)
    return "{:04d}-{:02d}".format(prev.year, prev.month)


def main(argv):
    dry_run = "--post" not in argv
    argv = [a for a in argv if a != "--post"]

    month = None
    if "--month" in argv:
        i = argv.index("--month")
        try:
            month = argv[i + 1]
            datetime.datetime.strptime(month, "%Y-%m")   # validate
        except (IndexError, ValueError):
            print("--month needs a YYYY-MM value, e.g. --month 2026-06")
            return 2
        del argv[i:i + 2]
    month = month or _prev_month()

    feed = None
    if "--feed" in argv:
        i = argv.index("--feed")
        if i + 1 >= len(argv):
            print("--feed needs a report name")
            return 2
        feed = argv[i + 1]
        del argv[i:i + 2]

    reports = list(config.MONTHLY_JSON_REPORTS)
    if feed:
        reports = [r for r in reports if r["name"] == feed]
        if not reports:
            print("no monthly feed named {!r}. feeds: {}".format(
                feed, ", ".join(r["name"] for r in config.MONTHLY_JSON_REPORTS)))
            return 2

    code = argv[0].strip().lower() if argv else "all"
    if code == "all":
        props = list(config.PROPERTIES)
    else:
        props = [p for p in config.PROPERTIES if p["code"] == code]
        if not props:
            print("unknown property {!r} (use bkds/bkdu/bkv/all)".format(code))
            return 2

    print("monthly feed(s) {} for {} — period {} — {}".format(
        [r["name"] for r in reports], ", ".join(p["code"].upper() for p in props),
        month, "DRY-RUN (nothing posted)" if dry_run else "POSTING to the dashboard"))

    lock = RunLock(config.RUN_LOCK_PATH)
    if not lock.acquire(wait=120):
        print("another VHP run is using Chrome right now — try again in a minute")
        return 1

    ok = fail = 0
    try:
        kill_stale_chromedrivers()
        for prop in props:
            try:
                driver = make_driver_attached(prop["debug_port"], config.DOWNLOADS_DIR)
            except Exception as exc:  # noqa: BLE001
                print("  could not attach to {} (port {}): {} — open-{}.bat + log in first"
                      .format(prop["code"], prop.get("debug_port"), vhp._describe(exc),
                              prop["code"]))
                fail += len(reports)
                continue
            for n, report in enumerate(reports):
                print("\n=== {} / {} {} ===".format(prop["code"].upper(), report["name"], month))
                try:
                    vhp.ensure_logged_in(driver, prop, None, report["url"])
                    daily_reports.run_monthly(driver, prop, report, config.DOWNLOADS_DIR,
                                              month, dry_run=dry_run)
                    ok += 1
                except vhp.LoginRequired as exc:
                    print("  LOGIN REQUIRED: {}".format(vhp._describe(exc)))
                    fail += 1
                    break   # session dropped — skip this property's remaining feeds
                except Exception as exc:  # noqa: BLE001
                    print("  FAILED: {}".format(vhp._describe(exc)))
                    fail += 1
                # Memory relief between the two heavy monthly reports (same as the
                # daily run): free the grid + a short pause before the next one.
                if n < len(reports) - 1:
                    try:
                        vhp.park_tab(driver)
                    except Exception:  # noqa: BLE001
                        pass
                    gap = config.pace("between_reports", 0.0)
                    if gap > 0:
                        time.sleep(gap)
    finally:
        lock.release()

    print("\n{} ok, {} failed.".format(ok, fail))
    if dry_run:
        print("DRY-RUN only — nothing was posted. Check the JSON above, then re-run "
              "with --post.")
    return 0 if fail == 0 and ok > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
