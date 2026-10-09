"""
test_scrape.py — quick manual test of the SCRAPE only (no dashboard upload).

Logs into VHP using a property's saved profile, pulls its report(s) via
"Print CSV", and tells you where each CSV landed. Use this to confirm the
scraping works before wiring up the upload.

Usage:
    C:\\Python38\\python.exe test_scrape.py bkv
    C:\\Python38\\python.exe test_scrape.py bkv yearly-forecast-of-room-occupancy

First arg  = property code (bkds / bkdu / bkv), defaults to bkv.
Second arg = a single report name (optional); default = all reports in config.
"""
import sys

import config
from browser import make_driver
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

    creds = config.get_credentials(code)
    driver = make_driver(prop["profile"], config.DOWNLOADS_DIR)
    ok = fail = 0
    try:
        print("Logging in as {} ...".format(code))
        try:
            ensure_logged_in(driver, prop, creds)
        except LoginRequired as exc:
            print("LOGIN FAILED: {}".format(exc))
            print("-> Create credentials.py (copy credentials.example.py) and put")
            print("   {}'s VHP username + password in it, then re-run.".format(code))
            return 1
        print("Logged in OK.")

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
        try:
            driver.quit()
        except Exception:  # noqa: BLE001
            pass

    print("\nDone: {} ok, {} failed.".format(ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
