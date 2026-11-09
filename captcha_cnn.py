"""
captcha_cnn.py — read VHP's login CAPTCHA with a small trained CNN.

The model (captcha_cnn.npz) was trained from real labelled samples of VHP's
strike-through numeric CAPTCHA. Inference here is PURE NUMPY (BatchNorm folded
into the conv/linear weights at export time), so the PC needs no torch/onnx — just
numpy + Pillow, which it already has.

predict() returns the 6 digits AND a confidence (the lowest per-digit softmax
probability). The caller submits ONLY when confidence is high and otherwise
REFRESHES the CAPTCHA (no login attempt), so a wrong submit — which could count
toward an account lockout — is rare. If the model file is missing or anything
fails, it returns (None, 0.0) and the caller falls back to OCR / a human.
"""
import os
import numpy as np

_DIR = os.path.dirname(os.path.abspath(__file__))
_MODEL_PATH = os.path.join(_DIR, "captcha_cnn.npz")
W, H = 150, 40

_P = None
_TRIED = False


def _load():
    global _P, _TRIED
    if _TRIED:
        return _P
    _TRIED = True
    try:
        if os.path.exists(_MODEL_PATH):
            d = np.load(_MODEL_PATH)
            # cast to float32 (weights may be stored float16 to shrink the file)
            _P = {k: d[k].astype(np.float32) for k in d.files}
    except Exception:  # noqa: BLE001 - missing/corrupt model -> fall back to OCR
        _P = None
    return _P


def available():
    """True if the trained CAPTCHA model loaded."""
    return _load() is not None


def _preprocess(img):
    """PIL RGB -> 40x150 float32 in [0,1] (R channel, min-max) — matches training."""
    im = img.convert("RGB").resize((W, H))
    a = np.asarray(im, dtype=np.float32)[:, :, 0]
    return (a - a.min()) / (a.max() - a.min() + 1e-6)


def _conv3(x, Wm, b):
    """3x3 'same' conv (pad 1). x:(C,H,W), Wm:(O,C,3,3), b:(O,). Loop im2col — the
    image is tiny, so this runs in well under a second."""
    C, Hh, Ww = x.shape
    O = Wm.shape[0]
    xp = np.pad(x, ((0, 0), (1, 1), (1, 1)))
    cols = np.empty((C * 9, Hh * Ww), np.float32)
    idx = 0
    for cc in range(C):
        for i in range(3):
            for j in range(3):
                cols[idx] = xp[cc, i:i + Hh, j:j + Ww].ravel()
                idx += 1
    return (Wm.reshape(O, C * 9) @ cols + b[:, None]).reshape(O, Hh, Ww)


def _pool2(x):
    """2x2 max-pool."""
    C, Hh, Ww = x.shape
    H2, W2 = Hh // 2, Ww // 2
    return x[:, :H2 * 2, :W2 * 2].reshape(C, H2, 2, W2, 2).max(axis=(2, 4))


def predict(img):
    """(digits_str, confidence) for a PIL CAPTCHA image, or (None, 0.0). Never raises.
    confidence = lowest of the 6 per-digit softmax max-probabilities (0..1)."""
    P = _load()
    if P is None:
        return None, 0.0
    try:
        x = _preprocess(img)[None]
        x = _pool2(np.maximum(0, _conv3(x, P["c1W"], P["c1b"])))
        x = _pool2(np.maximum(0, _conv3(x, P["c2W"], P["c2b"])))
        x = _pool2(np.maximum(0, _conv3(x, P["c3W"], P["c3b"])))
        v = x.ravel()
        h = np.maximum(0, v @ P["f1W"].T + P["f1b"])
        o = (h @ P["f2W"].T + P["f2b"]).reshape(6, 10)
        e = np.exp(o - o.max(1, keepdims=True))
        p = e / e.sum(1, keepdims=True)
        digits = "".join(str(int(r.argmax())) for r in p)
        conf = float(p.max(1).min())
        return digits, conf
    except Exception:  # noqa: BLE001 - any failure -> hand off
        return None, 0.0
