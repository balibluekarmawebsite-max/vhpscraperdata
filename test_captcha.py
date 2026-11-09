"""
test_captcha.py — check how well the bot can READ the VHP login CAPTCHA, WITHOUT
logging in or submitting anything (ZERO risk of locking the account).

Use it to judge the hit-rate BEFORE trusting auto-login. Get a property's window
onto the VHP /login page (this tool loads it for you), then:

    python test_captcha.py bkv 20     # read 20 CAPTCHAs (auto-refreshes between)

For each read it prints the predicted digits + a confidence, and whether that read
is confident enough to AUTO-SUBMIT (>= CAPTCHA_CNN_MIN_CONF). A healthy result:
most reads match what you see in the window, and the CONFIDENT ones are always
right. It only RELOADS the page to get a new CAPTCHA — it never submits.
"""
import os
import sys
import time

import captcha
import config
from browser import kill_stale_chromedrivers, make_driver_attached

LOGIN_URL = "https://e1-vhp.com/login"


def _new_captcha(driver):
    """Reload the login page (SAFE — never submits) for a fresh CAPTCHA."""
    try:
        driver.get(LOGIN_URL)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(1.3)


def main(argv):
    save = "--save" in argv
    argv = [a for a in argv if a != "--save"]
    if not argv:
        print("usage: python test_captcha.py <bkds|bkdu|bkv> [count] [--save]")
        return 2
    code = argv[0].strip().lower()
    count = int(argv[1]) if len(argv) > 1 else 10
    prop = next((p for p in config.PROPERTIES if p["code"] == code), None)
    if prop is None:
        print("unknown property {!r} (use bkds/bkdu/bkv)".format(code))
        return 2

    try:
        import captcha_cnn
        has_cnn = captcha_cnn.available()
    except Exception:  # noqa: BLE001
        has_cnn = False
    print("CAPTCHA reader: {}".format(
        "trained CNN model" if has_cnn else "OCR only (captcha_cnn.npz not found — did you git pull?)"))
    thr = float(getattr(config, "CAPTCHA_CNN_MIN_CONF", 0.97))
    print("auto-submit threshold (confidence): {:.2f}\n".format(thr))

    try:
        kill_stale_chromedrivers()
        driver = make_driver_attached(prop["debug_port"], config.DOWNLOADS_DIR)
    except Exception as exc:  # noqa: BLE001
        print("could not attach to the {} window on port {} (open-{}.bat first): {}"
              .format(code, prop.get("debug_port"), code, exc))
        return 1

    _new_captcha(driver)
    save_dir = os.path.join(config.BASE, "captcha_debug") if save else None
    if save_dir:
        os.makedirs(save_dir, exist_ok=True)

    print("Reading {} CAPTCHAs on the {} window (NOTHING is submitted).\n".format(count, code.upper()))
    confident = 0
    for i in range(count):
        digits, conf, source = captcha.read(driver)
        ok = bool(digits) and conf >= thr
        if ok:
            confident += 1
        tag = "CONFIDENT — would auto-submit" if ok else "low — would REFRESH (no submit)"
        print("  #{:<2} predicted: {:<8} conf {:.2f}   -> {}".format(
            i + 1, digits or "(none)", conf, tag))
        if save_dir:
            try:
                img = captcha._grab(driver)
                if img is not None:
                    img.save(os.path.join(save_dir, "cap_{:02d}_{}.png".format(i + 1, digits or "x")))
            except Exception:  # noqa: BLE001
                pass
        if i < count - 1:
            _new_captcha(driver)
    print("\n{} / {} reads were confident enough to auto-submit.".format(confident, count))
    print("CHECK: compare the predicted digits above against what you saw in the window.")
    print("If the CONFIDENT ones all matched, the reader is good to turn on.")
    if save_dir:
        print("Saved the CAPTCHA images to: {}".format(save_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
