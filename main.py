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
import sys

import config
from browser import make_driver
from uploader import upload
from vhp import is_logged_in, scrape_report

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


# ---------------------------------------------------------------------------
# Run one property
# ---------------------------------------------------------------------------
def run_property(logger, prop, reports):
    """Returns (ok_count, fail_count) for this property."""
    ok = fail = 0

    if prop["home_url"].startswith("PASTE-"):
        logger.error("[%s] home_url is still a placeholder — skipping", prop["code"])
        return 0, len(reports)

    driver = None
    try:
        driver = make_driver(prop["profile"], config.DOWNLOADS_DIR)
        driver.get(prop["home_url"])

        if not is_logged_in(driver):
            logger.error("re-login needed: %s — run `python login.py %s`",
                         prop["code"], prop["code"])
            return 0, len(reports)

        for report in reports:
            tag = "{}/{}".format(prop["code"], report["name"])
            try:
                path = scrape_report(driver, prop, report, config.DOWNLOADS_DIR)
                logger.info("[%s] downloaded -> %s", tag, path)
                status = upload(path, prop["code"], report["name"])
                logger.info("[%s] uploaded (HTTP %s)", tag, status)
                ok += 1
            except Exception as exc:  # noqa: BLE001 - isolate per-report failures
                fail += 1
                logger.exception("[%s] FAILED: %s", tag, exc)
    except Exception as exc:  # noqa: BLE001 - isolate per-property failures
        fail += len(reports) - ok
        logger.exception("[%s] property-level failure: %s", prop["code"], exc)
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:  # noqa: BLE001
                pass

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

    total_ok = total_fail = 0
    for prop in config.PROPERTIES:
        logger.info("=== %s (%s) ===", prop["name"], prop["code"])
        ok, fail = run_property(logger, prop, reports)
        total_ok += ok
        total_fail += fail

    logger.info("run done: %d ok, %d failed", total_ok, total_fail)
    if total_fail:
        send_alert(
            logger,
            "VHP scraper ({}): {} ok, {} FAILED — check logs.".format(
                args.cadence, total_ok, total_fail
            ),
        )
    # Non-zero exit if everything failed, so Task Scheduler can flag it.
    return 1 if total_ok == 0 and total_fail > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
