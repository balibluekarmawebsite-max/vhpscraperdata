"""
keepalive.py — stop the 3 VHP sessions from timing out.

The scraper attaches to Chrome windows you logged into by hand. If a window sits
idle too long, VHP logs the account out and the next scrape fails with "not
logged in". This script, run on a timer (Task Scheduler, every ~5 min), keeps each
window's session warm so VHP's inactivity clock keeps resetting.

    python keepalive.py            # confirm logged in, then HARD-REFRESH the page
                                   #   (a cache-bypassing reload = a real
                                   #   authenticated round-trip to VHP). This is
                                   #   what resets VHP's timer, and it also flushes
                                   #   page memory (helps the 'Aw, Snap!' crash).
    python keepalive.py --search   # ALSO press SEARCH (an even heavier guaranteed
                                   #   server round-trip) — use it only if the hard
                                   #   refresh still isn't enough to keep VHP alive.

Run it every 5 minutes (the Task Scheduler "VHP Keep-Alive" task); no code change
is needed to pick this up — just `git pull` and the existing task does the hard
refresh from its next run.

It does NOT export or upload anything — it only touches the page so the session
stays warm.

Coordination: it takes the same run lock as main.py / poller.py. If a scheduled
run or an on-demand job is using Chrome right now, keep-alive simply SKIPS this
tick — that run is already keeping the sessions alive, and we must never drive the
same window from two places at once.
"""
import argparse
import logging
import os
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler

import config
from browser import kill_stale_chromedrivers, make_driver_attached
from runlock import RunLock
from vhp import (LoginRequired, _captcha_reset, _on_login_url, _read_login_state,
                 attempt_auto_login, click_search, dismiss_session_expired,
                 open_report, refresh_page, wait_for_report_data)


def setup_logging():
    os.makedirs(config.LOGS_DIR, exist_ok=True)
    # Keep-alive runs often (every few minutes), so append to ONE rolling file
    # (capped) instead of a new file per run.
    log_path = os.path.join(config.LOGS_DIR, "keepalive.log")

    logger = logging.getLogger("vhp.keepalive")
    logger.setLevel(logging.INFO)
    logger.handlers[:] = []
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    fh = RotatingFileHandler(log_path, maxBytes=1000000, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def _describe(exc):
    first = (str(exc).strip().splitlines() or [""])[0]
    return "{}: {}".format(type(exc).__name__, first or "(no details)")


def touch_property(logger, prop, report, do_search):
    """Attach to one window and give it an authenticated nudge. Returns one of
    'ok' / 'logged_out' / 'error'. Never raises (the other properties still run)."""
    try:
        driver = make_driver_attached(prop["debug_port"], config.DOWNLOADS_DIR)
    except Exception as exc:  # noqa: BLE001 - window not open / not reachable
        logger.warning("[%s] cannot attach to its Chrome window on port %s "
                       "(is open-%s.bat running?): %s",
                       prop["code"], prop.get("debug_port"), prop["code"], _describe(exc))
        return "error"

    try:
        # Look at the window AS-IS first, WITHOUT navigating. If VHP is already
        # logged out, reloading would only fetch a fresh /login page -> a NEW
        # CAPTCHA, wiping any the human is part-way through typing. So:
        #   * expired dialog up -> click OK (no reload) and TRY AUTO-LOGIN (opt-in);
        #     this is a recovery WE initiated, so auto-submit is allowed.
        #   * already on /login (we did NOT create it) -> a human may be mid-login;
        #     leave it untouched and just report 'logged_out'. NEVER auto-submit here.
        # We only navigate (nudge to keep warm) once we're actually logged in.
        state = _read_login_state(driver)
        if state == "expired":
            closed = dismiss_session_expired(driver)  # click OK -> ready on login page
            if closed and attempt_auto_login(driver, prop, recovery=True, logger=logger):
                logger.info("[%s] auto-login succeeded — session restored", prop["code"])
                # fall through to the keep-warm nudge below
            elif closed:
                logger.warning("[%s] VHP is logged out — type the CAPTCHA + LOG IN in "
                               "its window (left ready on the login page)", prop["code"])
                return "logged_out"
            else:
                logger.warning("[%s] VHP session expired — click OK, then type the "
                               "CAPTCHA + LOG IN in its window", prop["code"])
                return "logged_out"
        elif state == "login" or (state == "unknown" and _on_login_url(driver)):
            logger.warning("[%s] VHP is on the login page — type the CAPTCHA + LOG IN "
                           "(left as-is for you; not reloading it)", prop["code"])
            return "logged_out"

        # Logged in (or just auto-logged-in). Navigate to keep the session warm. If
        # navigating reveals a fresh logout, that IS a recovery we initiated, so try
        # auto-login once; else hand off.
        try:
            open_report(driver, report)
        except LoginRequired:
            if attempt_auto_login(driver, prop, recovery=True, logger=logger):
                logger.info("[%s] auto-login succeeded after the nudge", prop["code"])
                try:
                    open_report(driver, report)
                except LoginRequired:
                    logger.warning("[%s] VHP logged out again — type the CAPTCHA + "
                                   "LOG IN in its window", prop["code"])
                    return "logged_out"
            else:
                logger.warning("[%s] VHP just logged out — type the CAPTCHA + LOG IN "
                               "in its window", prop["code"])
                return "logged_out"

        _captcha_reset(prop)  # confirmed logged-in -> re-arm auto-login for next time

        # The keep-alive itself: a cache-bypassing hard refresh forces a fresh,
        # authenticated round-trip to VHP (what actually resets its inactivity
        # timer) and flushes page memory. This is the 5-min "refresh" that keeps the
        # session logged in.
        refresh_page(driver)

        if do_search:
            try:
                click_search(driver)
                wait_for_report_data(driver, timeout=30)
                logger.info("[%s] session kept alive (hard refresh + SEARCH round-trip ok)",
                            prop["code"])
            except Exception as exc:  # noqa: BLE001 - SEARCH is best-effort here
                logger.info("[%s] hard-refreshed + logged in; SEARCH didn't finish "
                            "(fine for keep-alive): %s", prop["code"], _describe(exc))
        else:
            logger.info("[%s] session kept alive (page hard-refreshed, logged in)",
                        prop["code"])
        return "ok"
    except Exception as exc:  # noqa: BLE001 - isolate per-property failures
        logger.warning("[%s] keep-alive error: %s", prop["code"], _describe(exc))
        return "error"
    # No driver.quit() — leave the user's VHP window open.


def _notify_logged_out(logger, codes):
    """Pop a small, auto-dismissing desktop message when a VHP window is logged
    out, so a human knows to re-login (type the CAPTCHA). Rate-limited to once per
    30 min so it's never spammy, and it closes itself after 60s so it doesn't
    block other work. Windows only; best-effort."""
    if os.name != "nt" or not codes:
        return
    stamp = os.path.join(config.LOGS_DIR, ".last_logout_alert")
    try:
        if os.path.exists(stamp) and (time.time() - os.path.getmtime(stamp)) < 1800:
            return  # alerted recently — don't nag
    except OSError:
        pass
    text = ("VHP is logged out: {}.  Please open the Chrome window(s) and log in "
            "(type the CAPTCHA). The scraper can't pull data until you do."
            .format(", ".join(c.upper() for c in codes)))
    try:
        vbs = os.path.join(config.LOGS_DIR, "_logout_alert.vbs")
        with open(vbs, "w") as fh:
            fh.write('CreateObject("WScript.Shell").Popup "{}", 60, "VHP Scraper", 48'
                     .format(text.replace('"', '""')))
        subprocess.Popen(["wscript", vbs])  # non-blocking; auto-closes after 60s
        with open(stamp, "w") as fh:
            fh.write(str(int(time.time())))
    except Exception as exc:  # noqa: BLE001 - a notification must never break keep-alive
        logger.warning("could not show logout notification: %s", exc)


def main(argv):
    parser = argparse.ArgumentParser(description="Keep the VHP sessions logged in")
    parser.add_argument("--search", action="store_true",
                        help="after the hard refresh, ALSO press SEARCH (an even "
                             "heavier round-trip) — use only if the hard refresh "
                             "alone isn't enough to keep VHP alive")
    args = parser.parse_args(argv)

    logger = setup_logging()

    # Don't fight an active scrape for the windows — that run keeps sessions alive.
    lock = RunLock(config.RUN_LOCK_PATH)
    if not lock.acquire(wait=0):
        logger.info("a VHP run is active right now — skipping keep-alive "
                    "(that run is already keeping the sessions alive)")
        return 0

    try:
        kill_stale_chromedrivers()  # clear leftover drivers before attaching
        report = config.REPORTS[0]  # the forecast report URL = an authenticated page
        results = {}
        for prop in config.PROPERTIES:
            results[prop["code"]] = touch_property(logger, prop, report, args.search)
    finally:
        lock.release()

    oks = sum(1 for v in results.values() if v == "ok")
    logged_out = [c for c, v in results.items() if v == "logged_out"]
    logger.info("keep-alive done: %d alive, %d logged-out, %d error | %s",
                oks, len(logged_out), len(results) - oks - len(logged_out), results)
    if logged_out:
        _notify_logged_out(logger, logged_out)  # tell the human to re-login
    # Non-zero exit if any window is logged out, so Task Scheduler can flag it.
    return 1 if logged_out else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
