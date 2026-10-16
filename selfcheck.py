"""
selfcheck.py — one-shot health check for the whole VHP -> dashboard chain.

It prints a report of every link so we can see WHERE automatic delivery is
breaking, in this order:
    1. env        — token set? chromedriver present? which URLs?
    2. dashboard  — is the dashboard reachable from this PC?
    3. schedule   — did the scheduled tasks actually run, and with what result?
    4. VHP login  — are the 3 Chrome windows open and logged into VHP?
    5. recent logs— what did the last daily / weekly / poller runs actually say?

It is READ-ONLY: it does not upload any data or change settings. The only thing
it touches is navigating each Chrome window to a report page to check login
(same as the keep-alive), under the shared lock.

    python selfcheck.py

Send me everything it prints.
"""
import os
import subprocess
import sys
import time

import config


def hr(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


# ---------------------------------------------------------------------------
# 1. environment
# ---------------------------------------------------------------------------
def check_env():
    hr("1. ENVIRONMENT (token / driver / endpoints)")
    env_tok = os.environ.get("IMPORT_API_TOKEN", "")
    file_tok = ""
    try:
        file_tok = config._read_secret_file("import_token.txt", ".import_token")
    except Exception:  # noqa: BLE001
        pass
    # The RESOLVED token is what the scraper actually uses (env first, then file).
    resolved = config.UPLOAD.get("token", "")
    where = "env var" if env_tok else ("import_token.txt file" if file_tok else "NOWHERE")
    print("   IMPORT_API_TOKEN (env) : {}".format(
        "set (len {})".format(len(env_tok)) if env_tok else "not set in this environment"))
    print("   import_token.txt file  : {}".format(
        "found (len {})".format(len(file_tok)) if file_tok else "not present"))
    print("   -> token the scraper uses: {}".format(
        "OK, len {} (from {})".format(len(resolved), where) if resolved
        else "*** EMPTY — uploads WILL fail. Create import_token.txt ***"))
    drv = config.CHROMEDRIVER_PATH
    print("   chromedriver     : {}".format(
        "FOUND ({})".format(drv) if os.path.exists(drv) else "*** MISSING at {} ***".format(drv)))
    print("   import URL       : {}".format(config.UPLOAD.get("url")))
    print("   claim URL        : {}".format(config.JOBS.get("claim_url")))
    print("   downloads dir    : {}".format(config.DOWNLOADS_DIR))
    print("   logs dir         : {}".format(config.LOGS_DIR))


# ---------------------------------------------------------------------------
# 2. dashboard reachable?
# ---------------------------------------------------------------------------
def check_dashboard():
    hr("2. DASHBOARD REACHABLE?")
    try:
        import requests
    except Exception as exc:  # noqa: BLE001
        print("   requests not installed: {}".format(exc))
        return
    url = config.UPLOAD.get("url", "")
    base = "/".join(url.split("/")[:3]) if "//" in url else url  # scheme://host
    for target in (base, url):
        try:
            t0 = time.time()
            r = requests.get(target, timeout=20)
            print("   GET {} -> HTTP {} in {:.1f}s (reachable)".format(
                target, r.status_code, time.time() - t0))
        except Exception as exc:  # noqa: BLE001
            print("   GET {} -> ERROR: {} (NOT reachable)".format(target, _one(exc)))
    print("   (A real upload test is `run-daily.bat` — this only proves the PC can"
          " reach the server.)")


# ---------------------------------------------------------------------------
# 3. scheduled tasks — did they run?
# ---------------------------------------------------------------------------
def check_schedule():
    hr("3. SCHEDULED TASKS (did they run, and with what result?)")
    if os.name != "nt":
        print("   (not Windows — skipping schtasks)")
        return
    tasks = ["VHP Scraper Daily", "VHP Scraper Weekly", "VHP Keep-Alive", "VHP Poller"]
    keys = ("TaskName", "Status", "Last Run Time", "Last Result", "Next Run Time")
    for tn in tasks:
        try:
            out = subprocess.run(["schtasks", "/query", "/tn", tn, "/fo", "LIST", "/v"],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 universal_newlines=True)
            text = out.stdout or ""
        except Exception as exc:  # noqa: BLE001
            print("   {}: could not query ({})".format(tn, _one(exc)))
            continue
        if "ERROR" in text and "cannot find" in text.lower():
            print("   {}: NOT CREATED (task not found)".format(tn))
            continue
        vals = {}
        for line in text.splitlines():
            for k in keys:
                if line.strip().startswith(k + ":"):
                    vals[k] = line.split(":", 1)[1].strip()
        print("   {:18s} | last run: {} | last result: {} | next: {} | {}".format(
            tn, vals.get("Last Run Time", "?"), _result(vals.get("Last Result", "")),
            vals.get("Next Run Time", "?"), vals.get("Status", "?")))
    print("   (last result 0 = OK; 267011/'has not run' = never ran; 1 = the run"
          " reported a failure — check its log below.)")


def _result(code):
    code = (code or "").strip()
    m = {"0": "0 (OK)", "267011": "never run", "267009": "running",
         "1": "1 (FAILED — see log)"}
    return m.get(code, code or "?")


# ---------------------------------------------------------------------------
# 4. VHP windows logged in?
# ---------------------------------------------------------------------------
def check_vhp_login():
    hr("4. VHP WINDOWS — open + logged in?")
    try:
        import vhp
        from browser import kill_stale_chromedrivers, make_driver_attached
        from runlock import RunLock
    except Exception as exc:  # noqa: BLE001
        print("   cannot import browser modules (selenium installed?): {}".format(_one(exc)))
        return
    lock = RunLock(config.RUN_LOCK_PATH)
    if not lock.acquire(wait=60):
        print("   another VHP run is using Chrome right now (that's why the lock is"
              " held) — skipping the login check. Try again in a minute.")
        return
    try:
        kill_stale_chromedrivers()
        report = config.report_by_section("B") or config.REPORTS[0]
        for prop in config.PROPERTIES:
            code, port = prop["code"], prop.get("debug_port")
            try:
                driver = make_driver_attached(port, config.DOWNLOADS_DIR)
            except Exception as exc:  # noqa: BLE001
                print("   {:5s} (port {}): CANNOT ATTACH — is open-{}.bat running? ({})".format(
                    code, port, code, _one(exc)))
                continue
            try:
                vhp.open_report(driver, report)
                print("   {:5s} (port {}): LOGGED IN".format(code, port))
            except vhp.LoginRequired as exc:
                print("   {:5s} (port {}): LOGGED OUT / session expired -> re-login "
                      "this window. ({})".format(code, port, _one(exc)))
            except Exception as exc:  # noqa: BLE001
                print("   {:5s} (port {}): page didn't render ({})".format(code, port, _one(exc)))
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# 5. recent logs
# ---------------------------------------------------------------------------
def check_logs():
    hr("5. RECENT RUN LOGS (what the last runs actually did)")
    d = config.LOGS_DIR
    try:
        files = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".log")]
    except Exception as exc:  # noqa: BLE001
        print("   cannot read logs dir {}: {}".format(d, _one(exc)))
        return
    if not files:
        print("   no log files yet in {} — has any run happened?".format(d))
        return
    newest = {}
    for f in files:
        base = os.path.basename(f)
        kind = ("daily" if base.startswith("daily") else
                "weekly" if base.startswith("weekly") else
                "poller" if base.startswith("poller") else
                "keepalive" if base.startswith("keepalive") else "other")
        try:
            mt = os.path.getmtime(f)
        except OSError:
            continue
        if kind not in newest or mt > newest[kind][1]:
            newest[kind] = (f, mt)
    for kind in ("daily", "weekly", "poller", "keepalive"):
        if kind not in newest:
            print("\n   --- {}: (no log found) ---".format(kind))
            continue
        f, mt = newest[kind]
        print("\n   --- {}: {} (modified {}) ---".format(
            kind, os.path.basename(f), time.strftime("%Y-%m-%d %H:%M", time.localtime(mt))))
        for line in _tail(f, 18):
            print("   | " + line.rstrip())


def _tail(path, n):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.readlines()[-n:]
    except Exception as exc:  # noqa: BLE001
        return ["(could not read: {})".format(_one(exc))]


def _one(exc):
    s = str(exc).strip().splitlines()
    return "{}: {}".format(type(exc).__name__, s[0] if s else "")


def main():
    print("VHP scraper self-check — {}".format(time.strftime("%Y-%m-%d %H:%M:%S")))
    for fn in (check_env, check_dashboard, check_schedule, check_vhp_login, check_logs):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            print("   (section error: {})".format(_one(exc)))
    print("\n" + "=" * 70)
    print("Done. Send me everything above and I'll tell you exactly what's wrong.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
