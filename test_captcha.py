"""
test_captcha.py — check how well the bot can READ the VHP login CAPTCHA, WITHOUT
logging in or submitting anything (so there is ZERO risk of locking the account).

Use it to judge the hit-rate BEFORE trusting auto-login. Get a property's window
onto the VHP /login page (so the CAPTCHA is showing), then:

    python test_captcha.py bkds          # read the CAPTCHA once
    python test_captcha.py bkds 10        # read 10 CAPTCHAs (refresh between reads)

For each read it prints what the OCR saw and whether it was CONFIDENT (it would
auto-submit that one) or would hand off to a human. A healthy result is lots of
CONFIDENT reads whose digits actually match what you see on screen; if it's mostly
"not confident" or the confident ones are WRONG, leave auto-CAPTCHA off (create
logs/.no_auto_captcha) and keep typing them by hand.
"""
import sys

import captcha
import config
from browser import make_driver_attached


def main(argv):
    if not argv:
        print("usage: python test_captcha.py <bkds|bkdu|bkv> [count]")
        return 2
    code = argv[0].strip().lower()
    count = int(argv[1]) if len(argv) > 1 else 1
    prop = next((p for p in config.PROPERTIES if p["code"] == code), None)
    if prop is None:
        print("unknown property {!r} (use bkds/bkdu/bkv)".format(code))
        return 2

    if not captcha.ocr_available():
        print("OCR is NOT available: {}".format(captcha.ocr_unavailable_reason()))
        print("install it on this PC:")
        print("  pip install pytesseract pillow")
        print("  + the Tesseract-OCR engine (set TESSERACT_CMD to tesseract.exe if "
              "it's not on PATH)")
        return 1

    try:
        driver = make_driver_attached(prop["debug_port"], config.DOWNLOADS_DIR)
    except Exception as exc:  # noqa: BLE001
        print("could not attach to the {} window on port {} (open it with open-{}.bat "
              "and get it to the VHP login page first): {}"
              .format(code, prop.get("debug_port"), code, exc))
        return 1

    print("Reading the CAPTCHA on the {} window. Make sure it's on the VHP login "
          "page (CAPTCHA visible). NOTHING is submitted.".format(code.upper()))
    confident = 0
    for i in range(count):
        value = captcha.solve_captcha(driver, config.CAPTCHA_MIN_LEN, config.CAPTCHA_MAX_LEN)
        if value:
            confident += 1
            print("  read #{}: '{}'   -> CONFIDENT (would auto-submit)".format(i + 1, value))
        else:
            print("  read #{}: (not confident)   -> would hand to a human".format(i + 1))
        if i < count - 1:
            try:
                input("  click VHP's refresh-CAPTCHA icon, then press Enter for the "
                      "next read... ")
            except EOFError:
                break
    print("\n{} / {} reads were confident. Check that the confident ones MATCHED "
          "what you saw before trusting auto-login.".format(confident, count))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
