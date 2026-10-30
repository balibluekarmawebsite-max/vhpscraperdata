"""
main.py — orchestrates a run: loop properties x due reports.

    python main.py daily     # reports with cadence daily  (or both)
    python main.py weekly    # reports with cadence weekly (or both)
    python main.py both      # everything

Design goals:
  * One failure (a property or a single report) is logged and the run
    continues — the others still get their data.
  * Every run writes a timestamped log under logs/ AND echoes to the console
    (Task Scheduler's run.bat tees stdout into logs/run.log too).
"""
import argparse
import datetime
import logging
import os
import subprocess
import sys

import config
from browser import ensure_window, kill_stale_chromedrivers
from runlock import RunLock
from uploader import UploadError, upload
from vhp import LoginRequired, ensure_logged_in, scrape_report

VALID_CADENCES = ("daily", "weekly", "both")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logging(cadence):
    os.makedirs(config.LOGS_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_path = os.path.join(config.LOGS_DIR, "{}-{}.log".format(cadence, stamp))

    logger = logging.getLogger("vhp")
    logger.setLevel(logging.INFO)
    logger.handlers[:] = []  # avoid duplicate handlers on re-entry

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    logger.info("log file: %s", log_path)
    return logger


# ---------------------------------------------------------------------------
# Which reports run for this cadence?
# ---------------------------------------------------------------------------
def due_reports(cadence):
    out = []
    for r in config.REPORTS:
        rc = r.get("cadence", "daily")
        if cadence == "both" or rc == "both" or rc == cadence:
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# Alerting (optional)
# ---------------------------------------------------------------------------
def send_alert(logger, text):
    webhook = config.ALERTS.get("slack_webhook")
    if not webhook:
        return
    try:
        import requests

        requests.post(webhook, json={"text": text}, timeout=20)
    except Exception as exc:  # noqa: BLE001 - alerting must never crash the run
        logger.warning("alert failed: %s", exc)


def desktop_alert(text):
    """Pop a small, auto-dismissing Windows message so a FAILED run is noticed even
    with no Slack webhook configured (the run this is called from had 0 successes).
    Best-effort, Windows-only, never raises."""
    if os.name != "nt":
        return
    try:
        os.makedirs(config.LOGS_DIR, exist_ok=True)
        vbs = os.path.join(config.LOGS_DIR, "_run_alert.vbs")
        with open(vbs, "w") as fh:
            fh.write('CreateObject("WScript.Shell").Popup "{}", 180, "VHP Scraper", 48'
                     .format(text.replace('"', '""')))
        subprocess.Popen(["wscript", vbs])  # non-blocking; auto-closes after 180s
    except Exception:  # noqa: BLE001 - an alert must never break the run
        pass


# ---------------------------------------------------------------------------
# Run one property
# ---------------------------------------------------------------------------
def run_property(logger, prop, reports):
    """Returns (ok_count, fail_count) for this property."""
    ok = fail = 0

    if prop["home_url"].startswith("PASTE-"):
        logger.error("[%s] home_url is still a placeholder — skipping", prop["code"])
        return 0, len(reports)

    try:
        # Attach to the window — or auto-reopen it if it got closed (PC restart /
        # crash), so a scheduled run self-heals instead of failing the property.
        driver = ensure_window(prop, config.DOWNLOADS_DIR, logger)
    except Exception as exc:  # noqa: BLE001 - window closed AND couldn't be reopened
        logger.error("[%s] cannot attach to or reopen its Chrome window on port %s "
                     "(open it with open-%s.bat): %s",
                     prop["code"], prop.get("debug_port"), prop["code"], exc)
        return 0, len(reports)

    try:
        try:
            ensure_logged_in(driver, prop, None, reports[0]["url"])
        except LoginRequired as exc:
            logger.error("[%s] %s", prop["code"], exc)
            return 0, len(reports)

        for report in reports:
            tag = "{}/{}".format(prop["code"], report["name"])
            try:
                path = scrape_report(driver, prop, report, config.DOWNLOADS_DIR)
                logger.info("[%s] downloaded -> %s", tag, path)
                result = upload(path, prop["code"], report)
                logger.info("[%s] uploaded -> section %s, week %s (%s)",
                            tag, report.get("section"), result.get("week_ending"),
                            result.get("message") or "HTTP {}".format(result.get("_status")))
                for ep in result.get("_extra_posts", []):
                    logger.info("[%s] also posted -> %s (HTTP %s)",
                                tag, ep["url"], ep["status"])
                ok += 1
            except LoginRequired:
                logger.error("[%s] session dropped — log into its VHP window again",
                             prop["code"])
                fail = len(reports) - ok  # this + remaining reports can't run
                break
            except UploadError as exc:
                # Downloaded fine but the dashboard didn't accept the push.
                if exc.status == 409:
                    # That week is finalised/approved in the dashboard — this is
                    # intentional, not a failure. Skip (a human reopens it there
                    # to allow an update); don't raise a false "run failed" alert.
                    logger.warning("[%s] week is locked/approved in the dashboard "
                                   "— skipped (reopen it there to update): %s", tag, exc)
                else:
                    # 400/401/422/501 (needs a human) or 5xx/network after retries.
                    fail += 1
                    logger.error("[%s] downloaded OK but UPLOAD FAILED: %s", tag, exc)
            except Exception as exc:  # noqa: BLE001 - isolate per-report failures
                fail += 1
                logger.exception("[%s] FAILED: %s", tag, exc)
    except Exception as exc:  # noqa: BLE001 - isolate per-property failures
        fail += len(reports) - ok
        logger.exception("[%s] property-level failure: %s", prop["code"], exc)
    # No driver.quit() — leave the user's VHP window open for the next run.

    return ok, fail


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv):
    parser = argparse.ArgumentParser(description="VHP report scraper")
    parser.add_argument("cadence", choices=VALID_CADENCES, help="which reports to run")
    args = parser.parse_args(argv)

    logger = setup_logging(args.cadence)
    reports = due_reports(args.cadence)
    logger.info(
        "run start: cadence=%s, %d propert(ies), %d report(s) due",
        args.cadence, len(config.PROPERTIES), len(reports),
    )

    if not reports:
        logger.info("no reports due for cadence=%s — nothing to do", args.cadence)
        return 0

    # Serialise Chrome access with any on-demand poller / keep-alive run so the
    # two never kill each other's chromedriver. Poller jobs are short and release
    # between jobs, so this rarely waits; a dead lock (crashed run) is auto-stolen
    # by RunLock's stale check.
    lock = RunLock(config.RUN_LOCK_PATH)
    if not lock.acquire(wait=1800):
        # Only happens if a LIVE run held the lock for the whole wait. Proceeding
        # without it (and taskkilling the other run's chromedriver) would cause the
        # exact clash the lock prevents, so SKIP this run instead — the next
        # scheduled run retries. Exit non-zero so Task Scheduler flags it.
        logger.error("could not get the Chrome run lock (another VHP run held it "
                     "the whole wait) — skipping this run to avoid a clash; the "
                     "next scheduled run will retry")
        send_alert(logger, "VHP scraper ({}): skipped — Chrome was busy with "
                   "another run.".format(args.cadence))
        return 1

    total_ok = total_fail = 0
    try:
        kill_stale_chromedrivers()  # clear leftover drivers from previous runs

        for prop in config.PROPERTIES:
            lock.refresh()  # keep the lock fresh across a long multi-report run
            logger.info("=== %s (%s) ===", prop["name"], prop["code"])
            ok, fail = run_property(logger, prop, reports)
            total_ok += ok
            total_fail += fail
    finally:
        lock.release()

    logger.info("run done: %d ok, %d failed", total_ok, total_fail)
    if total_fail:
        send_alert(
            logger,
            "VHP scraper ({}): {} ok, {} FAILED — check logs.".format(
                args.cadence, total_ok, total_fail
            ),
        )
    # A COMPLETE failure (nothing uploaded) usually means the Chrome windows were
    # closed/unreachable — pop a desktop warning so it isn't missed (no webhook
    # needed), and exit non-zero so Task Scheduler flags the task red.
    if total_ok == 0 and total_fail > 0:
        desktop_alert("VHP {} run FAILED: 0 ok, {} failed. The 3 VHP Chrome windows "
                      "are probably closed/logged out — open them (open-all.bat), "
                      "log in, then re-run run-{}.bat.".format(
                          args.cadence, total_fail, args.cadence))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
