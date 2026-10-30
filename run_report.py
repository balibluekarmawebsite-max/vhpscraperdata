"""
run_report.py — run ONE report by name across the properties (scrape + upload),
on demand. Handy for TESTING a new report before scheduling it, or re-running one.

    python run_report.py cancelled-reservation-guest          # all 3 properties
    python run_report.py cancelled-reservation-guest bkds      # just BKDS
    python run_report.py yearly-forecast-pickup bkds --snapshot 2026-10-23
                                                               # backfill one date
    python run_report.py                                       # lists report names

It uploads for real (to whatever endpoint the report's config says), so use it to
confirm a new report works end-to-end before flipping its cadence to "daily".
--snapshot YYYY-MM-DD overrides the snapshotDate sent to a daily endpoint (e.g. to
re-pull a past day); without it, today's date is used. Takes the shared lock, so it
won't collide with the scheduled runs / poller.
"""
import datetime
import sys

import config
from browser import ensure_window, kill_stale_chromedrivers
from runlock import RunLock
from uploader import UploadError, upload
from vhp import LoginRequired, ensure_logged_in, scrape_report


def main(argv):
    names = [r["name"] for r in config.REPORTS]
    # Pull out the optional --snapshot YYYY-MM-DD flag from anywhere in argv.
    snapshot = None
    rest = []
    i = 0
    while i < len(argv):
        if argv[i] == "--snapshot" and i + 1 < len(argv):
            snapshot = argv[i + 1]
            i += 2
            continue
        rest.append(argv[i])
        i += 1
    argv = rest

    if not argv:
        print("usage: python run_report.py <report-name> [bkds|bkdu|bkv] "
              "[--snapshot YYYY-MM-DD]")
        print("reports: {}".format(", ".join(names)))
        return 2
    name = argv[0]
    only = argv[1].lower() if len(argv) > 1 else None

    snap_date = None
    if snapshot:
        try:
            snap_date = datetime.datetime.strptime(snapshot, "%Y-%m-%d").date()
        except ValueError:
            print("bad --snapshot {!r} — use YYYY-MM-DD".format(snapshot))
            return 2

    report = next((r for r in config.REPORTS if r["name"] == name), None)
    if report is None:
        print("no report named {!r}. reports: {}".format(name, ", ".join(names)))
        return 2
    props = [p for p in config.PROPERTIES if only is None or p["code"].lower() == only]
    if not props:
        print("no property {!r} (use bkds/bkdu/bkv)".format(only))
        return 2

    lock = RunLock(config.RUN_LOCK_PATH)
    if not lock.acquire(wait=120):
        print("another VHP run is using Chrome right now — try again in a minute")
        return 1

    ok = fail = 0
    try:
        kill_stale_chromedrivers()
        for prop in props:
            tag = "{}/{}".format(prop["code"], name)
            print("\n=== {} ===".format(tag))
            try:
                driver = ensure_window(prop, config.DOWNLOADS_DIR)
                ensure_logged_in(driver, prop, None, report["url"])
                path = scrape_report(driver, prop, report, config.DOWNLOADS_DIR)
                print("  downloaded -> {}".format(path))
                result = upload(path, prop["code"], report, today=snap_date)
                print("  uploaded -> HTTP {} ({})".format(
                    result.get("_status"), result.get("message") or "ok"))
                for ep in result.get("_extra_posts", []):
                    print("  + also posted -> {} (HTTP {})".format(ep["url"], ep["status"]))
                ok += 1
            except LoginRequired as exc:
                print("  LOGIN REQUIRED: {}".format(exc))
                fail += 1
            except UploadError as exc:
                print("  downloaded OK but UPLOAD FAILED: {}".format(exc))
                fail += 1
            except Exception as exc:  # noqa: BLE001
                print("  FAILED: {}".format(exc))
                fail += 1
    finally:
        lock.release()

    print("\ndone: {} ok, {} failed".format(ok, fail))
    return 0 if fail == 0 and ok > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
