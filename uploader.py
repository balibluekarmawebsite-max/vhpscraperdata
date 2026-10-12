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

    section = report.get("section")
    if not section:
        raise UploadError(
            "report {!r} has no dashboard 'section' in config — not uploading".format(
                report.get("name")))
    token = cfg.get("token")
    if not token:
        raise UploadError(
            "no dashboard token set — put it in the IMPORT_API_TOKEN env var "
            "(or config.UPLOAD['token'])")
    if not os.path.exists(path):
        raise UploadError("file to upload not found: {}".format(path))

    we = report.get("week_ending") or week_ending(today)
    source = report.get("source") or "vhp:{}".format(report.get("name", ""))
    headers = {"Authorization": "Bearer {}".format(token)}
    data = {
        "property": property_code.upper(),
        "section": section,
        "week_ending": we,
        "source": source,
    }

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
                    cfg["url"],
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
            # 2xx but NOT the API's JSON (e.g. the Next.js app shell on a cold
            # start right after a server restart). Treat as transient and retry;
            # the next (warm) request returns real JSON. Only fail if it never
            # does — so a genuine misroute isn't hidden, and data is never lost.
            ctype = resp.headers.get("content-type", "")
            snippet = (resp.text or "")[:140].replace("\n", " ").replace("\r", " ")
            if _retry_or_raise(
                    "got {} but not the API's JSON (content-type {!r}; likely the "
                    "web app / cold start, not /api/import JSON). First bytes: {}"
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
    #   python uploader.py <file.csv> <property> <section> [week_ending]
    import sys

    _args = sys.argv[1:]
    if len(_args) < 3:
        print("usage: python uploader.py <file.csv> <property BKDS|BKDU|BKV> "
              "<section B|C> [week_ending YYYY-MM-DD]")
        raise SystemExit(2)
    _report = {"name": "manual-test", "section": _args[2], "source": "manual-test"}
    if len(_args) > 3:
        _report["week_ending"] = _args[3]
    try:
        _res = upload(_args[0], _args[1], _report)
        print("OK:", _res)
    except UploadError as _exc:
        print("UPLOAD ERROR (status {}): {}".format(_exc.status, _exc))
        raise SystemExit(1)
