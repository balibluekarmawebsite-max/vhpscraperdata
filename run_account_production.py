"""
run_account_production.py — pull the MONTHLY "Account Production (YTD)" feed from
VHP's Forecast Room Production report and POST it to the Sales Highlight dashboard
(/api/sales/account-production). Independent of the scheduled daily/weekly runs.

    python run_account_production.py                       # prev month, all props, DRY-RUN
    python run_account_production.py bkds                  # prev month, BKDS, dry-run
    python run_account_production.py bkds --month 2026-10  # October, BKDS, dry-run
    python run_account_production.py all  --month 2026-10 --post   # October, all 3, POST

Default month = the PREVIOUS completed month (run in Nov -> reports Oct, YTD
Jan 1 .. Oct 31). Default is DRY-RUN (prints the JSON, posts nothing); add --post
to POST for real. Re-posting the same month is safe (the dashboard replaces that
month's accounts). Takes the shared run-lock, so it won't clash with the scheduled
runs / poller. Attaches to the already-open VHP Chrome windows (log in first).

Verify on the dashboard side:
    GET /api/sales/account-production?property=BKDS&period=2026-10
"""
import datetime
import sys

import config
import daily_reports
import vhp
from browser import kill_stale_chromedrivers, make_driver_attached
from runlock import RunLock


def _prev_month(today=None):
    """'YYYY-MM' of the previous completed month."""
    first = (today or datetime.date.today()).replace(day=1)
    last_prev = first - datetime.timedelta(days=1)
    return "{:04d}-{:02d}".format(last_prev.year, last_prev.month)


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
            print("--month needs a YYYY-MM value, e.g. --month 2026-10")
            return 2
        del argv[i:i + 2]
    month = month or _prev_month()

    code = argv[0].strip().lower() if argv else "all"
    report = config.monthly_json_report("forecast-room-production")
    if report is None:
        print("no 'forecast-room-production' report in config.MONTHLY_JSON_REPORTS")
        return 2
    if code == "all":
        props = list(config.PROPERTIES)
    else:
        props = [p for p in config.PROPERTIES if p["code"] == code]
        if not props:
            print("unknown property {!r} (use bkds/bkdu/bkv/all)".format(code))
            return 2

    print("account-production for {} — period {} (YTD) — {}".format(
        ", ".join(p["code"].upper() for p in props), month,
        "DRY-RUN (nothing posted)" if dry_run else "POSTING to the dashboard"))

    lock = RunLock(config.RUN_LOCK_PATH)
    if not lock.acquire(wait=120):
        print("another VHP run is using Chrome right now — try again in a minute")
        return 1

    ok = fail = 0
    try:
        kill_stale_chromedrivers()
        for prop in props:
            print("\n=== {} / forecast-room-production {} ===".format(
                prop["code"].upper(), month))
            try:
                driver = make_driver_attached(prop["debug_port"], config.DOWNLOADS_DIR)
                vhp.ensure_logged_in(driver, prop, None, report["url"])
                daily_reports.run_account_production(
                    driver, prop, report, config.DOWNLOADS_DIR, month, dry_run=dry_run)
                ok += 1
            except vhp.LoginRequired as exc:
                print("  LOGIN REQUIRED: {}".format(vhp._describe(exc)))
                fail += 1
            except Exception as exc:  # noqa: BLE001
                print("  FAILED: {}".format(vhp._describe(exc)))
                fail += 1
    finally:
        lock.release()

    print("\n{} ok, {} failed.".format(ok, fail))
    if dry_run:
        print("DRY-RUN only — nothing was posted. Check the JSON above, then re-run "
              "with --post.")
    return 0 if fail == 0 and ok > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
