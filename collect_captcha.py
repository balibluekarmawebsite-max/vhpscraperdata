"""
collect_captcha.py — gather LABELLED CAPTCHA examples so a reader can be trained.

Traditional OCR (captcha.py) can't read VHP's strike-through CAPTCHA reliably. The
fix is to train a reader on real examples of THIS CAPTCHA. This tool collects them
with almost no effort: it shows a fresh CAPTCHA in the VHP window, you type the
digits you SEE, and it saves the picture labelled with those digits — then loads a
new one. Do a few hundred, zip the folder, and send it to me; I'll train the reader
and send it back to slot into captcha.py.

    C:\\Python38\\python.exe collect_captcha.py --code bkv

While it runs (read the CAPTCHA from the VHP window, type into THIS terminal):
    <digits> + Enter   save this CAPTCHA with that label, then show a new one
    s + Enter          skip (unreadable / unsure), show a new one
    q + Enter          quit

SAFETY: it only RELOADS the login page to get a new CAPTCHA — it never fills in or
submits the login form, so it can never cause a failed login or lock the account.
Nothing is uploaded. Pictures are saved locally under captcha_samples\\ (git-ignored).

Tip: use a VHP window that is LOGGED OUT (showing the login page). If every window
is logged in there's no CAPTCHA to capture — log one out first, or just let one sit
on the login page.
"""
import argparse
import csv
import os
import sys
import time

import config
from browser import kill_stale_chromedrivers, make_driver_attached
from captcha import _find_captcha_image
from runlock import RunLock

LOGIN_URL = "https://e1-vhp.com/login"
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captcha_samples")


def _prop(code):
    return next((p for p in config.PROPERTIES if p["code"].lower() == code.lower()), None)


def _grab_png(driver):
    """PNG bytes of the current CAPTCHA image, or None if not found/readable."""
    el = _find_captcha_image(driver)
    if el is None:
        return None
    try:
        return el.screenshot_as_png
    except Exception:  # noqa: BLE001 - stale/again next loop
        return None


def _wait_new_captcha(driver, prev_png, timeout=8):
    """Wait until a CAPTCHA image shows whose bytes differ from prev_png (a genuinely
    new CAPTCHA after a reload). Returns the new PNG bytes, or the latest we saw."""
    end = time.time() + timeout
    latest = None
    while time.time() < end:
        latest = _grab_png(driver)
        if latest and latest != prev_png:
            return latest
        time.sleep(0.4)
    return latest


def _reload_new(driver, url, prev_png):
    """Reload the login page (SAFE — never submits) and return a fresh CAPTCHA."""
    try:
        driver.get(url)
    except Exception:  # noqa: BLE001 - transient nav hiccup; the wait retries
        pass
    time.sleep(1.2)
    return _wait_new_captcha(driver, prev_png)


def _existing_count(d):
    try:
        return len([f for f in os.listdir(d) if f.lower().endswith(".png")])
    except OSError:
        return 0


def main(argv):
    parser = argparse.ArgumentParser(description="Collect labelled VHP CAPTCHA samples")
    parser.add_argument("--code", default="bkv", help="which VHP window: bkds/bkdu/bkv")
    parser.add_argument("--url", default=LOGIN_URL, help="login page URL")
    parser.add_argument("--dir", default=OUT_DIR, help="folder to save samples into")
    args = parser.parse_args(argv)

    prop = _prop(args.code)
    if prop is None:
        print("no property {!r} (use bkds/bkdu/bkv)".format(args.code))
        return 2

    os.makedirs(args.dir, exist_ok=True)
    labels_csv = os.path.join(args.dir, "labels.csv")

    lock = RunLock(config.RUN_LOCK_PATH)
    if not lock.acquire(wait=120):
        print("another VHP run is using Chrome right now — try again in a minute")
        return 1
    try:
        kill_stale_chromedrivers()
        print("Attaching to the {} window (port {}) ...".format(
            prop["code"].upper(), prop["debug_port"]))
        driver = make_driver_attached(prop["debug_port"], config.DOWNLOADS_DIR)
        print("Opening the login page ...")
        driver.get(args.url)
        time.sleep(2.0)

        if _grab_png(driver) is None:
            print("\n*** No CAPTCHA found on this window. ***")
            print("This window is probably LOGGED IN. Log it out (or use a window")
            print("sitting on the VHP login page), then run this again.")
            return 1

        print("\n================  CAPTCHA collector  ================")
        print("Read the CAPTCHA in the {} VHP window, type the digits here.".format(
            prop["code"].upper()))
        print("  <digits>+Enter = save & next   |   s = skip   |   q = quit")
        print("Saving to: {}".format(args.dir))
        print("Aim for a few hundred. Nothing is uploaded or submitted.\n")

        start = _existing_count(args.dir)
        saved = skipped = 0
        prev = None
        while True:
            png = _grab_png(driver)
            if png is None:
                png = _reload_new(driver, args.url, prev)
            if png is None:
                print("  (couldn't read the CAPTCHA image — reloading)")
                prev = _reload_new(driver, args.url, prev)
                continue

            ans = input("digits (saved {} so far) > ".format(saved)).strip().lower()
            if ans == "q":
                break
            if ans in ("s", ""):
                skipped += 1
                prev = _reload_new(driver, args.url, png)
                continue
            if not ans.isdigit():
                print("  (digits only — or 's' to skip, 'q' to quit)")
                continue

            idx = start + saved + 1
            fname = "{}_{:05d}.png".format(ans, idx)
            try:
                with open(os.path.join(args.dir, fname), "wb") as fh:
                    fh.write(png)
                with open(labels_csv, "a", newline="") as fh:
                    csv.writer(fh).writerow([fname, ans])
                saved += 1
            except OSError as exc:
                print("  (couldn't save: {} — is the disk full?)".format(exc))
            prev = _reload_new(driver, args.url, png)

        total = _existing_count(args.dir)
        print("\nDone. Saved {} new, skipped {}. Total pictures in folder: {}".format(
            saved, skipped, total))
        print("Now ZIP the folder below and send it to me:")
        print("  {}".format(args.dir))
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
