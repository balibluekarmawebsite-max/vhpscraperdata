"""
poller.py — on-demand scraping driven by the dashboard.

The office PC can't be reached FROM the dashboard (it sits behind the office
router), so instead it reaches OUT: this script runs continuously and asks the
dashboard "any scrape requests?" every little while. For each request it scrapes
the exact period asked for, uploads the CSV through the same /api/import endpoint
the scheduled runs use, then tells the dashboard the job is done (or why it
failed).

    python poller.py            # run forever (the normal mode; run-poll.bat)
    python poller.py --drain     # run every queued job, then exit (for a timed
                                 #   task, or testing) — no idle looping
    python poller.py --once      # claim+run at most ONE job, then exit (testing)

Contract with the dashboard: VHP_JOBS_INTEGRATION.md.
  * POST <claim_url>            -> the oldest queued job (or none), marked running
  * POST <result_url(id)>       -> record the job's outcome (done / failed)
The data itself still goes through /api/import (uploader.upload), unchanged.

A file lock (runlock.RunLock) makes sure an on-demand job never runs at the same
time as the scheduled 6am/Friday run — they'd otherwise kill each other's
chromedriver (the "connection refused" clash). Whoever has the lock drives
Chrome; the other waits.
"""
import argparse
import datetime
import logging
import os
import sys
import time

import requests

import config
from browser import kill_stale_chromedrivers, make_driver_attached
from runlock import RunLock
from uploader import UploadError, upload, week_ending
from vhp import LoginRequired, _parse_iso, ensure_logged_in, scrape_report

# How long a claimed job will wait for Chrome to free up (the scheduled run may
# be using it). Healthy runs finish in a few minutes; a truly stuck lock is
# stolen by RunLock's stale check (default 30 min) regardless.
_LOCK_WAIT = 1500
_RETRY_STATUS = (429, 500, 502, 503, 504)
# Log an "alive, queue empty" line at most this often while idle (seconds).
_HEARTBEAT = 1800


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------
def setup_logging():
    os.makedirs(config.LOGS_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_path = os.path.join(config.LOGS_DIR, "poller-{}.log".format(stamp))

    logger = logging.getLogger("vhp.poller")
    logger.setLevel(logging.INFO)
    logger.handlers[:] = []
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    logger.info("poller log file: %s", log_path)
    return logger


def _describe(exc):
    first = (str(exc).strip().splitlines() or [""])[0]
    return "{}: {}".format(type(exc).__name__, first or "(no details)")


# ---------------------------------------------------------------------------
# dashboard HTTP (claim a job / report a result)
# ---------------------------------------------------------------------------
def _post_json(cfg, url, payload):
    """POST JSON with retries on network error / 429 / 5xx. Returns the Response
    (even a 4xx — the caller decides), or raises requests.RequestException if the
    network kept failing past the backoff budget."""
    backoffs = cfg.get("retry_backoffs", [2, 4, 8, 16])
    headers = {
        "Authorization": "Bearer {}".format(cfg.get("token", "")),
        "Content-Type": "application/json",
    }
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = requests.post(url, headers=headers, json=payload,
                                 timeout=cfg.get("timeout", 60))
        except requests.RequestException:
            if attempt <= len(backoffs):
                time.sleep(backoffs[attempt - 1])
                continue
            raise
        if resp.status_code in _RETRY_STATUS and attempt <= len(backoffs):
            time.sleep(backoffs[attempt - 1])
            continue
        return resp


def claim_job(logger, cfg):
    """Ask the dashboard for the next queued job. Returns the job dict, or None
    when the queue is empty / the call didn't succeed (we just try again later —
    a failed claim is never fatal)."""
    try:
        resp = _post_json(cfg, cfg["claim_url"], {"worker": cfg.get("worker", "")})
    except requests.RequestException as exc:
        logger.warning("claim: network error (will retry next poll): %s", exc)
        return None
    if resp.status_code == 204:
        return None  # no content = empty queue
    if not (200 <= resp.status_code < 300):
        logger.warning("claim: HTTP %s %s", resp.status_code, (resp.text or "")[:200])
        return None
    try:
        body = resp.json()
    except ValueError:
        logger.warning("claim: non-JSON response (%s): %s",
                       resp.status_code, (resp.text or "")[:160])
        return None
    if not isinstance(body, dict):
        return None
    job = body.get("job")
    if job is None and body.get("id"):   # tolerate a bare job object
        job = body
    return job if isinstance(job, dict) and job.get("id") is not None else None


def report_result(logger, cfg, job_id, status, message, result):
    """Tell the dashboard a job finished (done/failed). If this call itself fails
    the job stays 'running' on the dashboard — its stale-lease should re-queue it
    (see the spec), so no data is silently lost."""
    url = cfg["result_url"].format(id=job_id)
    payload = {"worker": cfg.get("worker", ""), "status": status, "message": message}
    if result is not None:
        payload["week_ending"] = result.get("week_ending")
        payload["written"] = result.get("written")
        payload["import"] = {k: v for k, v in result.items() if not k.startswith("_")}
    try:
        resp = _post_json(cfg, url, payload)
        if not (200 <= resp.status_code < 300):
            logger.error("result POST for job %s got HTTP %s: %s",
                         job_id, resp.status_code, (resp.text or "")[:200])
            return False
        return True
    except requests.RequestException as exc:
        logger.error("result POST for job %s failed (dashboard stale-lease should "
                     "re-queue it): %s", job_id, exc)
        return False


# ---------------------------------------------------------------------------
# turn a job into a scrape
# ---------------------------------------------------------------------------
def _find_property(code):
    want = (code or "").strip().upper()
    for p in config.PROPERTIES:
        if p["code"].upper() == want:
            return p
    return None


def _month_period(month):
    """{'start','end'} ISO range covering a whole 'YYYY-MM' month."""
    y, m = (int(x) for x in month.split("-")[:2])
    first = datetime.date(y, m, 1)
    nxt = datetime.date(y + 1, 1, 1) if m == 12 else datetime.date(y, m + 1, 1)
    last = nxt - datetime.timedelta(days=1)
    return {"start": first.isoformat(), "end": last.isoformat()}


def build_runtime_report(job):
    """Map a dashboard job to (report, property, error). `report` is a per-run
    copy of the base report for that section with the period / week / gate set.
    If `error` is not None the job must be failed WITHOUT scraping (bad request,
    or an unsupported mode) — nothing is uploaded."""
    section = (job.get("section") or "").strip().upper()
    base = config.report_by_section(section)
    if base is None:
        return None, None, "unknown section {!r} (expected B or C)".format(section)

    prop = _find_property(job.get("property") or job.get("property_code"))
    if prop is None:
        return None, None, "unknown property {!r}".format(job.get("property"))

    r = dict(base)          # shallow copy; keep checkboxes/segment_untick/url/etc
    r["_job_id"] = job.get("id")

    if section == "C":
        start = job.get("start") or job.get("start_date")
        end = job.get("end") or job.get("end_date")
        if not (start and end):
            return None, None, "section C job needs 'start' and 'end' (YYYY-MM-DD)"
        try:
            s, e = _parse_iso(start), _parse_iso(end)
        except Exception as exc:  # noqa: BLE001
            return None, None, "bad start/end date: {}".format(_describe(exc))
        if e < s:
            return None, None, "end {} is before start {}".format(end, start)
        r["period"] = {"start": s.isoformat(), "end": e.isoformat()}
        r["period_required"] = True  # never upload a wrong window
        r["week_ending"] = job.get("week_ending") or week_ending(e)
        return r, prop, None

    if section == "B":
        month = (job.get("month") or "").strip()
        current = datetime.date.today().strftime("%Y-%m")
        if month and month != current:
            if not config.JOBS.get("section_b_past_month_supported"):
                return None, None, (
                    "section B for a past month ({}) isn't wired yet — the VHP "
                    "yearly-forecast date control still needs inspecting. "
                    "(A section B job for the current month runs fine.)".format(month))
            r["period"] = _month_period(month)
            r["period_required"] = True  # skip rather than upload a wrong month
        # current month (or unspecified): default export already shows it.
        if month:
            r["_month"] = month
        if job.get("week_ending"):
            r["week_ending"] = job["week_ending"]
        return r, prop, None

    return None, None, "unsupported section {!r}".format(section)


def run_one_job(logger, job):
    """Scrape + upload for one job. Always returns (status, message, result) —
    never raises — so the main loop can always report an outcome."""
    try:
        report, prop, err = build_runtime_report(job)
    except Exception as exc:  # noqa: BLE001
        return "failed", "could not interpret job: {}".format(_describe(exc)), None
    if err:
        return "failed", err, None

    tag = "{}/{}#{}".format(prop["code"], report["name"], job.get("id"))
    try:
        driver = make_driver_attached(prop["debug_port"], config.DOWNLOADS_DIR)
    except Exception as exc:  # noqa: BLE001
        return ("failed",
                "cannot attach to the {} Chrome window on port {} — is it open "
                "(open-{}.bat) and logged into VHP? {}".format(
                    prop["code"], prop.get("debug_port"), prop["code"], _describe(exc)),
                None)
    try:
        try:
            ensure_logged_in(driver, prop, None, report["url"])
        except LoginRequired as exc:
            return "failed", "the {} window isn't logged into VHP: {}".format(prop["code"], exc), None
        try:
            path = scrape_report(driver, prop, report, config.DOWNLOADS_DIR)
        except LoginRequired as exc:
            return "failed", "VHP session dropped mid-scrape — log in again: {}".format(exc), None
        logger.info("[%s] downloaded -> %s", tag, path)
        try:
            result = upload(path, prop["code"], report)
        except UploadError as exc:
            if exc.status == 409:
                return ("failed",
                        "that week is locked/approved in the dashboard — reopen it "
                        "there to allow this update: {}".format(exc), None)
            return "failed", "downloaded OK but upload failed: {}".format(exc), None
        msg = result.get("message") or "HTTP {}".format(result.get("_status"))
        logger.info("[%s] uploaded -> section %s, week %s (%s)",
                    tag, report.get("section"), result.get("week_ending"), msg)
        return "done", msg, result
    except Exception as exc:  # noqa: BLE001
        logger.exception("[%s] job error", tag)
        return "failed", _describe(exc), None
    # No driver.quit() — leave the user's VHP window open for the next job.


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------
def _process_one(logger, cfg, job):
    """Take the Chrome lock, run the job, report the result."""
    jid = job.get("id")
    logger.info("claimed job %s: %s section %s %s", jid, job.get("property"),
                job.get("section"),
                job.get("month") or "{}..{}".format(job.get("start"), job.get("end")))
    lock = RunLock(config.RUN_LOCK_PATH)
    if not lock.acquire(wait=_LOCK_WAIT):
        logger.error("job %s: another VHP run held Chrome too long — reporting failed", jid)
        report_result(logger, cfg, jid, "failed",
                      "the office PC was busy with another VHP run for too long — "
                      "please re-request this scrape", None)
        return
    try:
        kill_stale_chromedrivers()
        status, message, result = run_one_job(logger, job)
    finally:
        lock.release()
    report_result(logger, cfg, jid, status, message, result)
    logger.info("job %s -> %s (%s)", jid, status, message)


def poll_loop(logger, cfg, drain=False, once=False):
    """Claim and run jobs. drain=True exits when the queue is empty; once=True
    exits after at most one job; otherwise loops forever, polling when idle."""
    interval = cfg.get("poll_interval", 120)
    last_beat = time.time()
    while True:
        job = claim_job(logger, cfg)
        if job is not None:
            _process_one(logger, cfg, job)
            if once:
                return
            time.sleep(2)   # a short beat, then check for the next queued job
            continue
        # queue empty
        if drain or once:
            logger.info("no jobs queued — exiting.")
            return
        if time.time() - last_beat >= _HEARTBEAT:
            logger.info("alive — no jobs queued (polling every %ss)", interval)
            last_beat = time.time()
        time.sleep(interval)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def main(argv):
    parser = argparse.ArgumentParser(description="VHP on-demand job poller")
    parser.add_argument("--drain", action="store_true",
                        help="run all queued jobs, then exit (no idle looping)")
    parser.add_argument("--once", action="store_true",
                        help="run at most one job, then exit (testing)")
    args = parser.parse_args(argv)

    logger = setup_logging()
    cfg = config.JOBS
    if not cfg.get("enabled"):
        logger.info("JOBS disabled in config.py — poller exiting.")
        return 0
    if not cfg.get("token"):
        logger.error("no token set (IMPORT_API_TOKEN or VHP_JOBS_TOKEN) — exiting.")
        return 2
    logger.info("VHP poller starting: worker=%s, poll=%ss, claim=%s",
                cfg.get("worker"), cfg.get("poll_interval"), cfg.get("claim_url"))
    try:
        poll_loop(logger, cfg, drain=args.drain, once=args.once)
    except KeyboardInterrupt:
        logger.info("poller stopped (Ctrl-C).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
