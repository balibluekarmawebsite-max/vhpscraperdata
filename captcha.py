"""
captcha.py — best-effort OCR of VHP's numeric login CAPTCHA.

The user opted in to the bot reading the CAPTCHA, after being warned that a WRONG
guess is a failed login and enough failed logins can LOCK the VHP account. So this
module is deliberately HIGH-PRECISION / LOW-RECALL: it returns a value only when
several independent preprocessings of the image AGREE on the same digits. Otherwise
it returns None and a human types the CAPTCHA. The caller (vhp.attempt_auto_login)
layers a cooldown + a circuit-breaker on top, so even a bad read can never hammer
the login.

Install ON THE PC:  pip install pytesseract pillow   (+ the Tesseract-OCR engine;
set TESSERACT_CMD to tesseract.exe if it's not on PATH). OPTIONAL, better accuracy:
pip install opencv-python numpy  (adds line-removal via connected components). If
anything is missing, solve_captcha() returns None and the bot falls back to a human.

Tune via config: CAPTCHA_LEN (exact digit count), CAPTCHA_MIN_VOTES (how many
preprocessings must agree), CAPTCHA_MIN_LEN / CAPTCHA_MAX_LEN (length range).
test_captcha.py --save dumps the image + every processed variant + each read so the
preprocessing can be tuned to the real CAPTCHA.
"""
import os
import re
from collections import Counter
from io import BytesIO

from selenium.webdriver.common.by import By

import config

# --- optional OCR deps: degrade gracefully to "manual" if not installed -----------
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

# --- optional OpenCV: better line removal; PIL-only fallback if absent ------------
try:
    import cv2
    import numpy as np

    _CV2 = True
except Exception:  # noqa: BLE001
    _CV2 = False


def ocr_available():
    """True if Pillow + pytesseract imported (the engine is exercised on first read)."""
    return _OCR_OK


def ocr_unavailable_reason():
    return None if _OCR_OK else repr(_OCR_IMPORT_ERR)


def cv2_available():
    return _CV2


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


def _grab(driver):
    """Screenshot the CAPTCHA element as an RGB PIL image, or None."""
    el = _find_captcha_image(driver)
    if el is None:
        return None
    return Image.open(BytesIO(el.screenshot_as_png)).convert("RGB")


def _pil_variants(img):
    """(name, image) preprocessings using Pillow only. Crucially includes BOTH
    POLARITIES: VHP's digits can be LIGHT on a dark teal box, which Tesseract (which
    expects dark text on a light background) can't read until inverted. Also a couple
    of thresholds and a line-thinning median filter, upscaled 3x for Tesseract."""
    out = []
    g = ImageOps.autocontrast(ImageOps.grayscale(img))
    w, h = g.size
    big = g.resize((max(w * 3, 1), max(h * 3, 1)))
    sources = [("", big), ("m", big.filter(ImageFilter.MedianFilter(3)))]
    for sname, src in sources:
        for thr in (120, 160):
            b = src.point(lambda p, t=thr: 255 if p > t else 0)
            out.append(("pil{}-t{}".format(sname, thr), b))                 # dark-on-light
            out.append(("pil{}-t{}-inv".format(sname, thr), ImageOps.invert(b)))  # inverted
    return out


def _cc_filter(bw):
    """Drop line-like / tiny connected components from a white-on-black binary
    (text = white). Removes residual strike-line fragments; keeps digit-height blobs."""
    H, W = bw.shape
    n, labels, stats, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
    keep = np.zeros_like(bw)
    for i in range(1, n):
        _x, _y, ww, hh, area = stats[i]
        if area < 25 or hh < H * 0.30:
            continue  # tiny / too short -> noise or a line fragment
        if ww > W * 0.55 and hh < H * 0.55:
            continue  # wide + short -> a (near-)horizontal strike line
        keep[labels == i] = 255
    return keep


def _cv2_variants(img):
    """(name, image) OpenCV preprocessings tuned for VHP's CAPTCHA: white digits on
    a blue box with THIN white strike-lines crossing the (thicker) digits.

    Otsu-threshold to white text on black (tried both ways so whichever polarity the
    digits are comes out right), then DE-LINE with a morphological OPENING / median
    blur — opening erases the thin lines while the thicker digit strokes survive —
    then connected-component clean-up. [] if cv2 is absent."""
    if not _CV2:
        return []
    out = []
    try:
        gray = np.array(ImageOps.grayscale(img))
        gray = cv2.resize(gray, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
        k2 = np.ones((2, 2), np.uint8)
        k3 = np.ones((3, 3), np.uint8)
        # Try both threshold polarities so "text = white" holds whether the digits
        # are brighter or darker than the box.
        for pol, flag in (("a", cv2.THRESH_BINARY), ("b", cv2.THRESH_BINARY_INV)):
            _, bw = cv2.threshold(gray, 0, 255, flag + cv2.THRESH_OTSU)
            deline = {
                "open2": cv2.morphologyEx(bw, cv2.MORPH_OPEN, k2),
                "open3": cv2.morphologyEx(bw, cv2.MORPH_OPEN, k3),
                "med3": cv2.medianBlur(bw, 3),
            }
            for vn, vb in deline.items():
                cleaned = _cc_filter(vb)
                out.append(("cv2-{}-{}".format(vn, pol),
                            Image.fromarray(cv2.bitwise_not(cleaned))))  # dark on light
    except Exception:  # noqa: BLE001 - cv2 hiccup -> just skip these variants
        return out
    return out


def _all_variants(img):
    return _pil_variants(img) + _cv2_variants(img)


# PSM 7 = single text line, 8 = single word, 13 = raw line. Different ones win on
# different images, so we try several and let the consensus decide.
_PSMS = (7, 8, 13)


def _ocr_variants(variants):
    """[(variant_name, psm, digits)] for every variant x PSM."""
    reads = []
    for name, vimg in variants:
        for psm in _PSMS:
            cfg = "--psm {} -c tessedit_char_whitelist=0123456789".format(psm)
            try:
                txt = pytesseract.image_to_string(vimg, config=cfg)
            except Exception:  # noqa: BLE001 - engine error on one call -> skip it
                continue
            reads.append((name, psm, re.sub(r"\D", "", txt or "")))
    return reads


def _pick(reads, lo, hi, exact, votes):
    """Consensus over the raw reads. Prefer EXACT-length reads (far fewer false
    positives); fall back to the length range. Returns the agreed value or None."""
    digits = [d for (_n, _p, d) in reads]
    pool = [d for d in digits if exact and len(d) == exact]
    if not pool:
        pool = [d for d in digits if lo <= len(d) <= hi]
    if not pool:
        return None
    ranked = Counter(pool).most_common(2)
    top, tv = ranked[0]
    if tv < votes:
        return None
    if len(ranked) > 1 and ranked[1][1] == tv:  # tie -> not confident
        return None
    return top


def _cfg_lengths():
    return (getattr(config, "CAPTCHA_MIN_LEN", 4),
            getattr(config, "CAPTCHA_MAX_LEN", 8),
            getattr(config, "CAPTCHA_LEN", 0),
            getattr(config, "CAPTCHA_MIN_VOTES", 3))


def solve_captcha(driver, min_len=None, max_len=None):
    """Return the CAPTCHA digits ONLY if confidently read (enough preprocessings
    agree), else None. Never raises."""
    if not _OCR_OK:
        return None
    lo, hi, exact, votes = _cfg_lengths()
    if min_len:
        lo = min_len
    if max_len:
        hi = max_len
    try:
        img = _grab(driver)
        if img is None:
            return None
        reads = _ocr_variants(_all_variants(img))
    except Exception:  # noqa: BLE001 - capture / engine failure -> let a human do it
        return None
    return _pick(reads, lo, hi, exact, votes)


def read_debug(driver, save_dir=None):
    """TUNING helper: returns (raw_image, [(variant, psm, digits)], consensus_value).
    If save_dir is given, writes the raw CAPTCHA and every processed variant there as
    PNGs. Never raises."""
    if not _OCR_OK:
        return None, [], None
    try:
        img = _grab(driver)
    except Exception:  # noqa: BLE001
        img = None
    if img is None:
        return None, [], None
    variants = _all_variants(img)
    reads = _ocr_variants(variants)
    lo, hi, exact, votes = _cfg_lengths()
    value = _pick(reads, lo, hi, exact, votes)
    if save_dir:
        try:
            os.makedirs(save_dir, exist_ok=True)
            img.save(os.path.join(save_dir, "captcha_raw.png"))
            for name, vimg in variants:
                vimg.convert("L").save(os.path.join(save_dir, "captcha_{}.png".format(name)))
        except Exception:  # noqa: BLE001 - saving is best-effort
            pass
    return img, reads, value


def _consensus(reads, min_votes=3):
    """Back-compat / unit-tested helper: value >=min_votes reads agree on, else None."""
    if not reads:
        return None
    ranked = Counter(reads).most_common(2)
    top, tv = ranked[0]
    if tv < min_votes:
        return None
    if len(ranked) > 1 and ranked[1][1] == tv:
        return None
    return top
