"""
login_safety.py — audit the PC settings that keep VHP logged in, so logouts stay
RARE and any slipped setting is caught early. It only REPORTS (changes nothing):

    C:\\Python38\\python.exe login_safety.py

Checks, each PASS / WARN / FAIL with how to fix it:
  1. Keep-Alive is running   — the 5-min task refreshed VHP in the last ~10 min
  2. VHP windows logged in   — the keep-alive's last tick saw all 3 'alive'
  3. Keep-Alive task exists   — the Task Scheduler job is present + its last result
  4. PC never sleeps          — sleep would freeze the keep-alive and drop VHP
  5. Windows Update no reboot  — the #1 cause of the weekend logout
  6. Startup reopen            — open-all.bat reopens the windows after a reboot

Run it anytime (especially after Windows updates or a reboot) to confirm VHP is
protected. It is safe to run while everything else is going.
"""
import os
import re
import subprocess
import time

import config

_RESULTS = []


def _add(level, title, detail="", fix=""):
    _RESULTS.append((level, title, detail, fix))


def _run(args):
    """Run a command, return its combined output text (or an error string)."""
    try:
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = p.communicate(timeout=30)
        return (out or b"").decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return "__ERR__ {}".format(exc)


# --- 1 + 2: keep-alive freshness + login status (read from keepalive.log) ---------
def check_keepalive_log():
    path = os.path.join(config.LOGS_DIR, "keepalive.log")
    if not os.path.exists(path):
        _add("FAIL", "Keep-Alive running", "no keepalive.log yet",
             "confirm the 'VHP Keep-Alive' scheduled task runs every 5 min")
        _add("WARN", "VHP windows logged in", "can't tell (no keep-alive log yet)")
        return
    age_min = (time.time() - os.path.getmtime(path)) / 60.0
    if age_min <= 10:
        _add("PASS", "Keep-Alive running",
             "last ran {:.0f} min ago (every 5 min)".format(age_min))
    else:
        _add("FAIL", "Keep-Alive running",
             "last ran {:.0f} min ago — the 5-min task may be stopped".format(age_min),
             "Task Scheduler -> 'VHP Keep-Alive' -> Enable/Run; check its History")

    # Parse the most recent "keep-alive done: N alive, M logged-out" line.
    alive = logged_out = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in reversed(fh.readlines()[-80:]):
                m = re.search(r"keep-alive done:\s*(\d+)\s*alive,\s*(\d+)\s*logged-out", line)
                if m:
                    alive, logged_out = int(m.group(1)), int(m.group(2))
                    break
    except Exception:  # noqa: BLE001
        pass
    if logged_out is None:
        _add("WARN", "VHP windows logged in", "couldn't read the last keep-alive result")
    elif logged_out == 0:
        _add("PASS", "VHP windows logged in", "last tick: {} alive, 0 logged-out".format(alive))
    else:
        _add("FAIL", "VHP windows logged in",
             "last tick: {} alive, {} LOGGED OUT".format(alive, logged_out),
             "open the window(s) + type the CAPTCHA + LOG IN (run open-all.bat)")


# --- 3: the keep-alive scheduled task exists --------------------------------------
def check_keepalive_task():
    if os.name != "nt":
        _add("WARN", "Keep-Alive task", "(not Windows — skipping schtasks)")
        return
    out = _run(["schtasks", "/query", "/tn", "VHP Keep-Alive", "/fo", "LIST", "/v"])
    if out.startswith("__ERR__") or "ERROR" in out.upper() or "VHP Keep-Alive" not in out:
        _add("FAIL", "Keep-Alive task", "'VHP Keep-Alive' task not found",
             "create a Task Scheduler job running run-keepalive.bat every 5 minutes")
        return
    last = re.search(r"Last Run Time:\s*(.+)", out)
    res = re.search(r"Last Result:\s*(.+)", out)
    _add("PASS", "Keep-Alive task",
         "present (last run {}, result {})".format(
             (last.group(1).strip() if last else "?"), (res.group(1).strip() if res else "?")))


# --- 4: the PC never sleeps -------------------------------------------------------
def check_power():
    if os.name != "nt":
        _add("WARN", "PC never sleeps", "(not Windows — skipping powercfg)")
        return
    # Query the 'Sleep after' setting; fall back to the whole Sleep subgroup, then
    # the full scheme — Win7's alias support / output wording varies.
    out = ""
    for args in (["powercfg", "/query", "SCHEME_CURRENT", "SUB_SLEEP", "STANDBYIDLE"],
                 ["powercfg", "/query", "SCHEME_CURRENT", "SUB_SLEEP"],
                 ["powercfg", "/query"]):
        out = _run(args)
        if not out.startswith("__ERR__") and re.search(r"Setting Index:\s*0x", out):
            break
    # Looser match: any "... Setting Index: 0x...." line (covers 'Current AC/DC
    # Power Setting Index' and minor wording differences).
    idx = re.findall(r"Setting Index:\s*0x([0-9a-fA-F]+)", out)
    if idx and all(int(v, 16) == 0 for v in idx):
        _add("PASS", "PC never sleeps", "sleep timeout = Never")
    elif idx:
        secs = max(int(v, 16) for v in idx)
        _add("FAIL", "PC never sleeps", "sleeps after ~{} min".format(secs // 60),
             "Control Panel -> Power Options: set 'Put the computer to sleep' = Never")
    else:
        snippet = " ".join(out.split())[:200] if not out.startswith("__ERR__") else out
        _add("WARN", "PC never sleeps",
             "couldn't parse powercfg (you set it to Never earlier, so this is "
             "likely fine). Raw: {}".format(snippet),
             "verify Control Panel -> Power Options: 'Put the computer to sleep' = Never")


# --- 5: Windows Update won't auto-reboot ------------------------------------------
def check_windows_update():
    if os.name != "nt":
        _add("WARN", "Windows Update no auto-reboot", "(not Windows — skipping)")
        return
    au = None
    for key in (r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update",
                r"HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"):
        out = _run(["reg", "query", key, "/v", "AUOptions"])
        m = re.search(r"AUOptions\s+REG_DWORD\s+0x([0-9a-fA-F]+)", out)
        if m:
            au = int(m.group(1), 16)
            break
    # AUOptions: 2 = notify download, 3 = auto-download + notify install (SAFE,
    # won't reboot on its own), 4 = auto-install (REBOOTS). 5 = admin decides.
    if au in (2, 3):
        _add("PASS", "Windows Update no auto-reboot",
             "AUOptions={} (downloads, but you choose when to install)".format(au))
    elif au == 4:
        _add("FAIL", "Windows Update no auto-reboot",
             "AUOptions=4 (auto-installs + reboots — this logged VHP out before)",
             "Control Panel -> Windows Update -> Change settings -> "
             "'Download updates but let me choose whether to install them'")
    else:
        _add("WARN", "Windows Update no auto-reboot",
             "couldn't confirm the setting (AUOptions={})".format(au),
             "verify: Control Panel -> Windows Update -> Change settings is "
             "'Download updates but let me choose whether to install them'")


# --- 6: Startup reopens the windows after a reboot --------------------------------
def check_startup():
    if os.name != "nt":
        _add("WARN", "Startup reopen", "(not Windows — skipping)")
        return
    appdata = os.environ.get("APPDATA", "")
    folder = os.path.join(appdata, "Microsoft", "Windows", "Start Menu",
                          "Programs", "Startup")
    try:
        items = [f for f in os.listdir(folder)
                 if f.lower().endswith((".lnk", ".bat", ".cmd"))]
    except Exception:  # noqa: BLE001
        items = []
    if items:
        _add("PASS", "Startup reopen",
             "Startup has: {} (confirm one opens open-all.bat)".format(", ".join(items)))
    else:
        _add("WARN", "Startup reopen", "no shortcut found in the Startup folder",
             "put a shortcut to open-all.bat in shell:startup so the 3 VHP windows "
             "reopen after a reboot")


def main():
    print("VHP login-safety audit — {}\n".format(
        time.strftime("%Y-%m-%d %H:%M:%S")))
    for fn in (check_keepalive_log, check_keepalive_task, check_power,
               check_windows_update, check_startup):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - one check must not abort the rest
            _add("WARN", fn.__name__, "check errored: {}".format(exc))

    icon = {"PASS": "[ OK ]", "WARN": "[WARN]", "FAIL": "[FAIL]"}
    for level, title, detail, fix in _RESULTS:
        print("{}  {}".format(icon.get(level, "[??]"), title))
        if detail:
            print("        {}".format(detail))
        if fix and level != "PASS":
            print("        FIX: {}".format(fix))
    fails = sum(1 for r in _RESULTS if r[0] == "FAIL")
    warns = sum(1 for r in _RESULTS if r[0] == "WARN")
    print("\n{} FAIL, {} WARN, {} OK.".format(
        fails, warns, len(_RESULTS) - fails - warns))
    if fails == 0 and warns == 0:
        print("VHP login is fully protected. Logouts should be rare; the keep-alive "
              "will catch and report any that slip through.")
    elif fails == 0:
        print("No failures — but review the WARN lines above to be fully covered.")
    else:
        print("Fix the [FAIL] items above — those are what let VHP log out.")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
