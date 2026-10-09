"""
test_scrape.py — quick manual test of the SCRAPE only (no dashboard upload).

Attaches to the Chrome window you opened for a property (with open-<code>.bat)
and already logged into, pulls its report(s) via "Print CSV", and tells you
where each CSV landed.

BEFORE running:
    1. Open the property's window:   open-bkv.bat
    2. Log into VHP in that window (solve the CAPTCHA), leave it open.

Usage:
    C:\\Python38\\python.exe test_scrape.py bkv
    C:\\Python38\\python.exe test_scrape.py bkv yearly-forecast-of-room-occupancy
"""
import sys

import config
from browser import kill_stale_chromedrivers, make_driver_attached
from vhp import LoginRequired, ensure_logged_in, scrape_report


def main(argv):
    code = (argv[0] if argv else "bkv").strip().lower()
    only_report = argv[1].strip() if len(argv) > 1 else None

    prop = next((p for p in config.PROPERTIES if p["code"] == code), None)
    if prop is None:
        print("Unknown property code: {!r}".format(code))
        return 2

    reports = config.REPORTS
    if only_report:
        reports = [r for r in reports if r["name"] == only_report]
        if not reports:
            print("No report named {!r} in config.REPORTS".format(only_report))
            return 2

    kill_stale_chromedrivers()
    try:
        driver = make_driver_attached(prop["debug_port"], config.DOWNLOADS_DIR)
    except Exception as exc:  # noqa: BLE001
        print("Could not attach to the {} Chrome window on port {}.".format(
            code, prop["debug_port"]))
        print("-> First run  open-{}.bat  and log into VHP in that window.".format(code))
        print("   ({})".format(exc))
        return 1

    ok = fail = 0
    try:
        try:
            # creds=None: we never auto-login here; you logged in by hand.
            ensure_logged_in(driver, prop, None, reports[0]["url"])
        except LoginRequired as exc:
            print("NOT LOGGED IN: {}".format(exc))
            return 1
        print("Attached to your logged-in {} window OK.".format(code))

        for r in reports:
            print("--- scraping {} / {} ...".format(code, r["name"]))
            try:
                path = scrape_report(driver, prop, r, config.DOWNLOADS_DIR)
                print("    SAVED -> {}".format(path))
                ok += 1
            except LoginRequired:
                print("    session dropped mid-run for {}".format(code))
                fail += 1
                break
            except Exception as exc:  # noqa: BLE001
                fail += 1
                print("    FAILED: {}".format(exc))
    finally:
        # Do NOT quit — that would close your VHP window. Just stop driving it.
        pass

    print("\nDone: {} ok, {} failed.".format(ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
