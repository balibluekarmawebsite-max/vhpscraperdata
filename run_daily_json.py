"""
run_daily_json.py — run (and verify) the two derived daily JSON feeds on demand,
WITHOUT touching the scheduled runs. Use it to eyeball the aggregated numbers
before trusting the feed, then to post for real.

    python run_daily_json.py reservation bkv            # scrape + PRINT json (no post)
    python run_daily_json.py departure  bkv             # scrape + PRINT json (no post)
    python run_daily_json.py both       bkv             # both feeds, print only
    python run_daily_json.py both       all --post      # all 3 properties, POST for real

Default is DRY-RUN (prints the JSON, posts nothing). Add --post to actually POST
to the dashboard. It attaches to the already-open VHP Chrome window(s) and uses
the same run-lock as the scheduled runs (so it won't clash with them).

BACK-FILL a specific day with --date YYYY-MM-DD: the feed reports yesterday/today/
tomorrow RELATIVE to that date, so --date 2026-11-09 posts departures for 08/09/10
Nov. (The 'Expected Departure' view only allows To Date = today or later, so the
dated run's "tomorrow" must be today or in the future; older days come from the
'Departed' range view.)

    python run_daily_json.py departure bkdu --date 2026-11-09 --post   # back-fill 8/9/10

Verify on the dashboard side with:
    GET /api/daily/reservation?property=BKV
"""
import datetime
import sys

import config
import daily_reports
import vhp
from browser import kill_stale_chromedrivers, make_driver_attached
from runlock import RunLock

_KINDS = {
    "reservation": ["reservation-daily"],
    "departure": ["room-departure-daily"],
    "both": ["reservation-daily", "room-departure-daily"],
}


def _run_one(prop, report_names, dry_run, today=None):
    ok = fail = 0
    try:
        driver = make_driver_attached(prop["debug_port"], config.DOWNLOADS_DIR)
    except Exception as exc:  # noqa: BLE001
        print("  could not attach to {} (port {}): {} — open-{}.bat + log in first"
              .format(prop["code"], prop.get("debug_port"), vhp._describe(exc),
                      prop["code"]))
        return 0, len(report_names)

    for name in report_names:
        report = config.daily_json_report(name)
        print("\n=== {} / {} ===".format(prop["code"].upper(), name))
        try:
            # Make sure we're logged in on this report's page before scraping.
            vhp.ensure_logged_in(driver, prop, None, report["url"])
            daily_reports.run(driver, prop, report, config.DOWNLOADS_DIR,
                              today=today, dry_run=dry_run)
            ok += 1
        except vhp.LoginRequired as exc:
            print("  LOGIN REQUIRED: {}".format(vhp._describe(exc)))
            fail += 1
        except Exception as exc:  # noqa: BLE001
            print("  FAILED: {}".format(vhp._describe(exc)))
            fail += 1
    return ok, fail


def main(argv):
    dry_run = "--post" not in argv
    argv = [a for a in argv if a != "--post"]
    # Optional --date YYYY-MM-DD: report yesterday/today/tomorrow relative to it
    # (for back-filling a missed day). Default = the real today.
    today = None
    if "--date" in argv:
        i = argv.index("--date")
        try:
            today = datetime.datetime.strptime(argv[i + 1], "%Y-%m-%d").date()
        except (IndexError, ValueError):
            print("--date needs a YYYY-MM-DD value, e.g. --date 2026-11-09")
            return 2
        del argv[i:i + 2]
    if len(argv) < 2 or argv[0] not in _KINDS:
        print("usage: python run_daily_json.py <reservation|departure|both> "
              "<bkds|bkdu|bkv|all> [--date YYYY-MM-DD] [--post]")
        print("  default is DRY-RUN (prints JSON, posts nothing); --post actually posts.")
        print("  --date YYYY-MM-DD back-fills that day's window (yesterday/today/tomorrow).")
        return 2

    report_names = _KINDS[argv[0]]
    code = argv[1].strip().lower()
    if code == "all":
        props = list(config.PROPERTIES)
    else:
        props = [p for p in config.PROPERTIES if p["code"] == code]
        if not props:
            print("unknown property {!r} (use bkds/bkdu/bkv/all)".format(code))
            return 2

    import daily_json as _dj
    win = _dj.prev_today_tomorrow(today)
    print("{} feed(s) {} for {} — window {}..{} ({}) — {}".format(
        ", ".join(report_names),
        "DRY-RUN (no post)" if dry_run else "POSTING",
        ", ".join(p["code"].upper() for p in props),
        win[0], win[-1],
        "today={}".format(today.isoformat()) if today else "today=now",
        "nothing is posted" if dry_run else "WILL POST to the dashboard"))

    lock = RunLock(config.RUN_LOCK_PATH)
    if not lock.acquire(wait=120):
        print("another VHP run is using Chrome right now — try again in a minute")
        return 1
    total_ok = total_fail = 0
    try:
        kill_stale_chromedrivers()
        for prop in props:
            ok, fail = _run_one(prop, report_names, dry_run, today=today)
            total_ok += ok
            total_fail += fail
    finally:
        lock.release()

    print("\n{} ok, {} failed.".format(total_ok, total_fail))
    if dry_run:
        print("DRY-RUN only — nothing was posted. Check the JSON above, then re-run "
              "with --post (or wait for the wired daily run).")
    return 1 if total_fail and not total_ok else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
