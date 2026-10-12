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
import re
import sys
import time

import requests

import config
from browser import kill_stale_chromedrivers, make_driver_attached
from runlock import RunLock
from uploader import UploadError, upload
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
def _post_json(cfg, url, payload, retry=True):
    """POST JSON. With retry=True, retries network error / 429 / 5xx with backoff.
    With retry=False, a SINGLE attempt (used for /api/jobs/claim, which is NOT
    idempotent — each successful claim consumes a different job, so a retry after
    a lost response would strand the first job). Returns the Response (even a 4xx
    — the caller decides), or raises requests.RequestException on a network failure
    past the backoff budget."""
    backoffs = cfg.get("retry_backoffs", [2, 4, 8, 16]) if retry else []
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
        # retry=False: a lost response after the server committed the claim must
        # NOT be retried (it would consume a second job). A miss just returns None
        # and we try again on the next poll, so nothing is lost.
        resp = _post_json(cfg, cfg["claim_url"], {"worker": cfg.get("worker", "")},
                         retry=False)
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


def report_result(logger, cfg, job_id, status, message, result, week_ending=None):
    """Tell the dashboard a job finished (done/failed). If this call itself fails
    the job stays 'running' on the dashboard — its stale-lease should re-queue it
    (see the spec), so no data is silently lost.

    week_ending is the week the scraper actually filled (computed in
    build_runtime_report) — we report THAT, since /api/import's JSON response
    doesn't echo it back."""
    url = cfg["result_url"].format(id=job_id)
    payload = {"worker": cfg.get("worker", ""), "status": status, "message": message}
    we = week_ending or (result.get("week_ending") if result else None)
    if we is not None:
        payload["week_ending"] = we
    if result is not None:
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
        # Section C is uploaded per WEEK — a full replace of ONE Fri->Thu week's
        # segment rows. A multi-week range would export in full but could carry
        # only one week label -> silently wrong-week data. So require exactly one
        # Fri->Thu week: end is a Thursday (weekday 3) and start the Friday 6 days
        # before. To back-fill several weeks, the dashboard queues one job each.
        if e.weekday() != 3 or (e - s).days != 6:
            return None, None, (
                "section C is uploaded one week at a time (Fri->Thu): 'start' must "
                "be a Friday and 'end' the following Thursday (got {}..{} = {} day(s)). "
                "Queue one job per week to back-fill a month.".format(
                    s.isoformat(), e.isoformat(), (e - s).days + 1))
        we = job.get("week_ending")
        if we:
            try:
                we_d = _parse_iso(we)
            except Exception as exc:  # noqa: BLE001
                return None, None, "bad week_ending: {}".format(_describe(exc))
            if we_d != e:
                return None, None, (
                    "week_ending {} must equal the week's Thursday {} for section C"
                    .format(we, e.isoformat()))
        r["period"] = {"start": s.isoformat(), "end": e.isoformat()}
        r["period_required"] = True            # never upload a wrong window
        r["week_ending"] = e.isoformat()        # the Thursday that ends this week
        return r, prop, None

    if section == "B":
        # Section B fills a calendar YEAR (a Jan..Dec table). The report shows 12
        # months from its start month, so we start it at JANUARY of the requested
        # year -> the export is a clean Jan..Dec of that year, which the dashboard
        # maps straight onto its month rows. The user can pick any date in the
        # year they want to (back)fill; we take the year from it.
        month = (job.get("month") or "").strip()
        year = job.get("year")
        if month:
            if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
                return None, None, "bad month {!r} (expected YYYY-MM)".format(month)
            year = int(month[:4])
        elif year:
            try:
                year = int(str(year)[:4])
            except Exception:  # noqa: BLE001
                return None, None, "bad year {!r} (expected YYYY)".format(year)
        else:
            year = datetime.date.today().year
        r["period"] = {"month": "{:04d}-01".format(year)}  # January of that year
        r["period_required"] = True   # skip rather than upload a wrong window
        r["_year"] = year
        if job.get("week_ending"):
            r["week_ending"] = job["week_ending"]
        return r, prop, None

    return None, None, "unsupported section {!r}".format(section)


def _scrape_and_upload(logger, report, prop, job_id):
    """Attach, scrape the already-built+validated report, upload. Always returns
    (status, message, result) — never raises — so the caller can always report an
    outcome. Assumes the Chrome lock is already held by the caller."""
    tag = "{}/{}#{}".format(prop["code"], report["name"], job_id)
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
    """Validate the job, then (only if it needs Chrome) take the lock, scrape,
    and report the result."""
    jid = job.get("id")
    # Validate FIRST — no Chrome, no lock. A bad/unsupported job (unknown section
    # or property, bad dates, a gated-off past-month B) fails instantly with the
    # real reason, instead of waiting out the lock and taskkilling chromedrivers.
    try:
        report, prop, err = build_runtime_report(job)
    except Exception as exc:  # noqa: BLE001
        report_result(logger, cfg, jid, "failed",
                      "could not interpret job: {}".format(_describe(exc)), None,
                      week_ending=job.get("week_ending"))
        logger.info("job %s -> failed (bad job: %s)", jid, _describe(exc))
        return
    if err:
        report_result(logger, cfg, jid, "failed", err, None,
                      week_ending=job.get("week_ending"))
        logger.info("job %s -> failed (%s)", jid, err)
        return

    we = report.get("week_ending")
    logger.info("claimed job %s: %s section %s %s", jid, job.get("property"),
                job.get("section"),
                job.get("month") or "{}..{}".format(job.get("start"), job.get("end")))

    lock = RunLock(config.RUN_LOCK_PATH)
    if not lock.acquire(wait=_LOCK_WAIT):
        logger.error("job %s: another VHP run held Chrome too long — reporting failed", jid)
        report_result(logger, cfg, jid, "failed",
                      "the office PC was busy with another VHP run for too long — "
                      "please re-request this scrape", None, week_ending=we)
        return
    try:
        kill_stale_chromedrivers()
        lock.refresh()  # keep the lock fresh so a slow scrape isn't judged stale
        status, message, result = _scrape_and_upload(logger, report, prop, jid)
    finally:
        lock.release()
    report_result(logger, cfg, jid, status, message, result, week_ending=we)
    logger.info("job %s -> %s (%s)", jid, status, message)


def poll_loop(logger, cfg, drain=False, once=False):
    """Claim and run jobs. drain=True exits when the queue is empty; once=True
    exits after at most one job; otherwise loops forever, polling when idle.

    The body is wrapped so ONE unexpected error can never take the watcher down —
    it logs, waits, and keeps polling. A dead poller silently stops answering all
    scrape requests, which is worse than a logged hiccup."""
    interval = cfg.get("poll_interval", 120)
    last_beat = time.time()
    while True:
        did_job = False
        try:
            job = claim_job(logger, cfg)
            if job is not None:
                did_job = True
                _process_one(logger, cfg, job)
                if once:
                    return
            elif drain or once:
                logger.info("no jobs queued — exiting.")
                return
            elif time.time() - last_beat >= _HEARTBEAT:
                logger.info("alive — no jobs queued (polling every %ss)", interval)
                last_beat = time.time()
        except Exception as exc:  # noqa: BLE001 - the daemon must never die on one error
            logger.exception("poll cycle error (continuing): %s", exc)
        time.sleep(2 if did_job else interval)


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
