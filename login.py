"""
login.py — one-time manual login per property.

Usage (run once per property, on the Windows 7 PC):

    C:\\Python38\\python.exe login.py bkds
    C:\\Python38\\python.exe login.py bkdu
    C:\\Python38\\python.exe login.py bkv

It opens Chrome with that property's profile. Log in by hand in the window,
then come back to the console and press Enter. The session cookies are saved in
the profile folder, so later unattended runs start already logged in.
"""
import sys

import config
from browser import make_driver


def find_property(code):
    for prop in config.PROPERTIES:
        if prop["code"] == code:
            return prop
    return None


def main(argv):
    if len(argv) != 1:
        codes = ", ".join(p["code"] for p in config.PROPERTIES)
        print("Usage: python login.py <property-code>   (one of: {})".format(codes))
        return 2

    code = argv[0].strip().lower()
    prop = find_property(code)
    if prop is None:
        print("Unknown property code: {!r}".format(code))
        return 2

    if prop["home_url"].startswith("PASTE-"):
        print(
            "home_url for {} is still a placeholder. Edit config.py first.".format(
                code
            )
        )
        return 2

    driver = make_driver(prop["profile"], config.DOWNLOADS_DIR, headless=False)
    try:
        driver.get(prop["home_url"])
        print("\nChrome is open for {} ({}).".format(prop["name"], code))
        print("Log in by hand in the browser window, then return here.")
        input("Press Enter once you are fully logged in... ")
        print("Session saved to profile: {}".format(prop["profile"]))
    finally:
        driver.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
