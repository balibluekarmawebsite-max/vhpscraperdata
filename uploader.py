"""
uploader.py — POST a downloaded VHP report to the BK Sales Dashboard import API.

Contract: VHP_INTEGRATION.md.  One POST per report to /api/import with the file
plus `property`, `section` and `week_ending`.  The dashboard runs its own AI
extraction, so we do NOT parse columns or touch the database here.

Section mapping (set per report in config.REPORTS):
    yearly-forecast-of-room-occupancy -> "B"  (monthly ACTUALS only)
    reservation-by-creation-date      -> "C"  (market-segment rows)
"""
import datetime
import os
import time

import requests

import config


class UploadError(Exception):
    """An upload failure that should be logged/alerted (non-retryable, or retries
    exhausted). Carries the HTTP status (or None for a client-side problem)."""

    def __init__(self, message, status=None):
        super(UploadError, self).__init__(message)
        self.status = status


def week_ending(today=None):
    """The Thursday that ends the current reporting week (weeks run Fri->Thu),
    as 'YYYY-MM-DD'. Matches the dashboard URL /weekly/<property>/<week_ending>/.
    (Python weekday(): Mon=0 .. Thu=3 .. Sun=6.)"""
    d = today or datetime.date.today()
    return (d - datetime.timedelta(days=(d.weekday() - 3) % 7)).isoformat()


# Transient HTTP statuses worth retrying with backoff (per VHP_INTEGRATION §8).
_RETRY_STATUS = (429, 500, 502, 503, 504)


def _resolve_token(custom, cfg):
    """The auth token for a custom upload endpoint. Prefer the endpoint's OWN token
    (literal `token`, env var named by `token_env`, or git-ignored file named by
    `token_file`); fall back to the shared /api/import token. This lets an endpoint
    like /api/daily/cancellation use a DIFFERENT token when it rejects the import
    one (HTTP 401)."""
    if custom.get("token"):
        return custom["token"]
    env = custom.get("token_env")
    if env and os.environ.get(env):
        return os.environ[env].strip()
    fname = custom.get("token_file")
    if fname:
        val = config._read_secret_file(fname)
        if val:
            return val
    return cfg.get("token")


def upload(path, property_code, report, cfg=None, today=None):
    """Upload one report file to the dashboard import API.

    path          : the downloaded CSV (already renamed by vhp.rename_download).
    property_code : 'bkds' / 'bkdu' / 'bkv' (sent upper-cased).
    report        : the config.REPORTS entry — provides `section` (required) and
                    optional `source` / `week_ending`.

    Returns the API's JSON dict on success (with an added '_status'). Retries
    network errors and 429/5xx with exponential backoff; raises UploadError on a
    non-retryable failure (400/401/409/413/422/501) or once retries are spent.
    """
    if cfg is None:
        cfg = config.UPLOAD

    if not os.path.exists(path):
        raise UploadError("file to upload not found: {}".format(path))

    # A report may define its OWN endpoint via report['upload'] — e.g. the
    # cancellation report posts the raw CSV to /api/daily/cancellation with just
    # `property` + the file (the dashboard parses + de-dups it). A custom endpoint
    # may also name its OWN auth token (token / token_env / token_file); otherwise
    # we use the shared /api/import contract (property/section/week_ending/source)
    # and the shared token.
    custom = report.get("upload")
    if custom:
        url = custom.get("url") or cfg["url"]
        token = _resolve_token(custom, cfg)
        data = {"property": property_code.upper()}
        data.update(custom.get("extra_fields", {}) or {})
        require_ok = bool(custom.get("require_ok", False))
    else:
        section = report.get("section")
        if not section:
            raise UploadError(
                "report {!r} has no dashboard 'section' (and no 'upload') in config "
                "— not uploading".format(report.get("name")))
        url = cfg["url"]
        token = cfg.get("token")
        data = {
            "property": property_code.upper(),
            "section": section,
            "week_ending": report.get("week_ending") or week_ending(today),
            "source": report.get("source") or "vhp:{}".format(report.get("name", "")),
        }
        require_ok = True

    if not token:
        raise UploadError(
            "no dashboard token for {} — put it in import_token.txt in C:\\vhp-scraper "
            "(or set the report's upload.token_file / token_env)".format(url))

    headers = {"Authorization": "Bearer {}".format(token)}

    backoffs = cfg.get("retry_backoffs", [2, 4, 8, 16])
    attempt = 0
    timings = []  # per-attempt "status in Ns" — shows fast-crash vs slow-timeout

    def _retry_or_raise(msg, status=None):
        """Sleep + continue if we still have retries, else raise UploadError."""
        if attempt <= len(backoffs):
            time.sleep(backoffs[attempt - 1])
            return True
        raise UploadError("{} after {} attempt(s) [{}]".format(
            msg, attempt, ", ".join(timings)), status=status)

    while True:
        attempt += 1
        t0 = time.time()
        try:
            with open(path, "rb") as fh:
                resp = requests.post(
                    url,
                    headers=headers,
                    data=data,
                    files={"file": (os.path.basename(path), fh, "text/csv")},
                    timeout=cfg.get("timeout", 180),
                )
        except requests.RequestException as exc:  # network / client timeout
            timings.append("net-err {:.1f}s".format(time.time() - t0))
            if _retry_or_raise("network error ({})".format(exc)):
                continue

        elapsed = time.time() - t0
        status = resp.status_code
        timings.append("{} {:.1f}s".format(status, elapsed))

        if 200 <= status < 300:
            try:
                body = resp.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and body.get("ok"):
                body["_status"] = status
                body["_timings"] = timings
                return body
            # 2xx but not {"ok": true}.
            ctype = resp.headers.get("content-type", "")
            snippet = (resp.text or "")[:140].replace("\n", " ").replace("\r", " ")
            looks_html = "html" in ctype.lower() or (resp.text or "").lstrip()[:1] == "<"
            if not require_ok and not looks_html:
                # Custom endpoint (e.g. /api/daily/cancellation): accept any real
                # 2xx — it's idempotent and may not return the {"ok":true} shape.
                result = body if isinstance(body, dict) else {}
                result["ok"] = True
                result["_status"] = status
                result["_timings"] = timings
                return result
            # Otherwise a 2xx that's actually the Next.js app shell (cold start
            # after a restart). Treat as transient and retry; the next (warm)
            # request returns real JSON. Fail only if it never does — so a genuine
            # misroute isn't hidden, and data is never lost.
            if _retry_or_raise(
                    "got {} but not the API's JSON (content-type {!r}; likely the "
                    "web app / cold start). First bytes: {}"
                    .format(status, ctype, snippet),
                    status=status):
                continue

        # Non-2xx. Retry transient (429/5xx/network); hard-fail the rest.
        detail = _error_detail(resp)
        if status in _RETRY_STATUS:
            if _retry_or_raise("HTTP {} from dashboard ({})".format(status, detail),
                               status=status):
                continue
        raise UploadError(
            "HTTP {} from dashboard: {} [{}]".format(status, detail, ", ".join(timings)),
            status=status)


def _error_detail(resp):
    try:
        j = resp.json()
        return j.get("error") or j
    except ValueError:
        return (resp.text or "")[:300]


if __name__ == "__main__":
    # Quick manual test of ONE upload, independent of scraping:
    #   set IMPORT_API_TOKEN=...   (Windows: set IMPORT_API_TOKEN=xxxx)
    #   python uploader.py <file.csv> <property> <section> [week_ending] [--once]
    #
    # --once = a SINGLE attempt, no retries. Use it while debugging the server so
    #          a failing upload doesn't retry 5x (and crash-loop the app 5x).
    import sys

    _argv = sys.argv[1:]
    _once = "--once" in _argv
    _args = [a for a in _argv if a != "--once"]
    if len(_args) < 3:
        print("usage: python uploader.py <file.csv> <property BKDS|BKDU|BKV> "
              "<section B|C> [week_ending YYYY-MM-DD] [--once]")
        raise SystemExit(2)
    _report = {"name": "manual-test", "section": _args[2], "source": "manual-test"}
    if len(_args) > 3:
        _report["week_ending"] = _args[3]
    _cfg = None
    if _once:
        _cfg = dict(config.UPLOAD, retry_backoffs=[])  # no retries
        print("(--once: single attempt, no retries)")
    try:
        _res = upload(_args[0], _args[1], _report, cfg=_cfg)
        print("OK:", _res)
    except UploadError as _exc:
        print("UPLOAD ERROR (status {}): {}".format(_exc.status, _exc))
        raise SystemExit(1)
