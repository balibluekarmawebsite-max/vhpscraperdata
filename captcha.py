"""
captcha.py — best-effort OCR of VHP's numeric login CAPTCHA.

The user opted in to the bot reading the CAPTCHA itself, after being warned that a
WRONG guess is a failed login and that enough failed logins can LOCK the VHP
account. So this module is deliberately HIGH-PRECISION / LOW-RECALL: it returns a
value only when several independent preprocessings of the image AGREE on the same
digits. Otherwise it returns None and a human types the CAPTCHA. The caller
(vhp.attempt_auto_login) layers a cooldown + a circuit-breaker on top, so even a
bad read can never hammer the login.

Dependencies to install ON THE PC (not needed anywhere else):
    pip install pytesseract pillow
plus the Tesseract-OCR engine itself (a Windows installer from the Tesseract
project). If PATH doesn't find it, set the TESSERACT_CMD env var to the full path
of tesseract.exe. If ANY of this is missing, solve_captcha() simply returns None
and the bot falls back to a human — it never crashes the scraper.
"""
import os
import re
from collections import Counter
from io import BytesIO

from selenium.webdriver.common.by import By

# --- optional OCR deps: degrade gracefully to "manual" if they're not installed ---
try:
    import pytesseract
    from PIL import Image, ImageFilter, ImageOps

    _cmd = os.environ.get("TESSERACT_CMD")
    if _cmd:
        pytesseract.pytesseract.tesseract_cmd = _cmd
    _OCR_OK = True
    _OCR_IMPORT_ERR = None
except Exception as _exc:  # noqa: BLE001 - missing Pillow / pytesseract / engine
    _OCR_OK = False
    _OCR_IMPORT_ERR = _exc


def ocr_available():
    """True if Pillow + pytesseract imported (the Tesseract engine itself is only
    exercised on the first real read)."""
    return _OCR_OK


def ocr_unavailable_reason():
    return None if _OCR_OK else repr(_OCR_IMPORT_ERR)


# Where VHP paints the CAPTCHA. Try the obvious hooks, then fall back to the
# image/canvas/svg sitting just before the "Enter CAPTCHA" input.
_CAPTCHA_IMG_SELECTORS = [
    "img[src*='captcha' i]",
    "img[alt*='captcha' i]",
    "[class*='captcha' i] img",
    "[class*='captcha' i] canvas",
    "[class*='captcha' i] svg",
    "canvas[class*='captcha' i]",
    "[id*='captcha' i] img",
    "[id*='captcha' i] canvas",
]

_CAPTCHA_IMG_FALLBACK_XPATH = (
    "(//input[contains(translate(@placeholder,"
    "'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'CAPTCHA')]"
    "/preceding::*[self::img or self::canvas or self::svg])[last()]"
)


def _find_captcha_image(driver):
    for sel in _CAPTCHA_IMG_SELECTORS:
        for el in driver.find_elements(By.CSS_SELECTOR, sel):
            try:
                if el.is_displayed() and (el.size or {}).get("width", 0) > 20:
                    return el
            except Exception:  # noqa: BLE001 - stale element, keep looking
                continue
    for el in driver.find_elements(By.XPATH, _CAPTCHA_IMG_FALLBACK_XPATH):
        try:
            if el.is_displayed() and (el.size or {}).get("width", 0) > 20:
                return el
        except Exception:  # noqa: BLE001
            continue
    return None


def _binarize(img, thr):
    return img.point(lambda p: 255 if p > thr else 0)


def _variants(img):
    """A few cheap preprocessings of the CAPTCHA. The strike-through lines are thin,
    so a median filter knocks them back while the thick digits survive; upscaling
    3x helps Tesseract on small text. Returns a list of 1-bit-ish PIL images."""
    g = ImageOps.autocontrast(ImageOps.grayscale(img))
    w, h = g.size
    big = g.resize((max(w * 3, 1), max(h * 3, 1)))
    return [
        _binarize(big, 140),                                  # plain threshold
        _binarize(big.filter(ImageFilter.MedianFilter(3)), 140),  # de-lined
        _binarize(big, 110),                                  # stronger threshold
    ]


_DIGITS_CFG = "--psm 7 -c tessedit_char_whitelist=0123456789"


def _ocr_digits(img):
    txt = pytesseract.image_to_string(img, config=_DIGITS_CFG)
    return re.sub(r"\D", "", txt or "")


def solve_captcha(driver, min_len=4, max_len=8):
    """Return the CAPTCHA digits ONLY if confidently read (>=2 preprocessings agree
    on the same value, within the length range), else None. Never raises."""
    if not _OCR_OK:
        return None
    try:
        el = _find_captcha_image(driver)
        if el is None:
            return None
        img = Image.open(BytesIO(el.screenshot_as_png)).convert("RGB")
    except Exception:  # noqa: BLE001 - capture failed -> let a human do it
        return None

    reads = []
    try:
        for v in _variants(img):
            d = _ocr_digits(v)
            if min_len <= len(d) <= max_len:
                reads.append(d)
    except Exception:  # noqa: BLE001 - tesseract engine missing/erroring at runtime
        return None

    return _consensus(reads, min_votes=2)


def _consensus(reads, min_votes=2):
    """Return the value at least `min_votes` of the reads agree on exactly, else
    None. This is the precision gate: we only auto-submit a CAPTCHA that several
    independent preprocessings read identically."""
    if not reads:
        return None
    value, votes = Counter(reads).most_common(1)[0]
    return value if votes >= min_votes else None
