"""
test_safeguards.py — fast checks on the auto-CAPTCHA SAFETY logic (the circuit
breaker + the OCR precision gate). No browser needed. Run it after any change to
the login/auto-CAPTCHA code to confirm the lock-out protections still hold:

    C:\\Python38\\python.exe test_safeguards.py

It uses a throwaway temp folder, so it never touches your real logs/ state.
"""
import json
import os
import sys
import tempfile


def main():
    import config
    tmp = tempfile.mkdtemp()
    config.LOGS_DIR = tmp
    config.AUTO_CAPTCHA_KILL_FILE = os.path.join(tmp, ".no_auto_captcha")
    config.AUTO_CAPTCHA_COOLDOWN_SEC = 2
    config.AUTO_CAPTCHA_MAX_FAILURES = 2
    config.AUTO_CAPTCHA_ENABLED = True
    os.environ.pop("VHP_NO_AUTO_CAPTCHA", None)

    import time

    import captcha
    import vhp

    results = []

    def check(cond, msg):
        results.append(bool(cond))
        print(("PASS " if cond else "FAIL ") + msg)

    P = {"code": "bkds"}
    Q = {"code": "bkdu"}
    path = vhp._cap_state_path(P)

    check(vhp._captcha_blocked(P) is None, "fresh (missing file) -> not blocked")

    st, persisted = vhp._captcha_record_failure(P)
    check(persisted, "record_failure persisted (atomic write)")
    check("cooldown" in (vhp._captcha_blocked(P) or ""), "after a submit -> cooldown blocks")
    check(not os.path.exists(path + ".tmp"), "no leftover .tmp after write")

    time.sleep(2.1)
    st, persisted = vhp._captcha_record_failure(P)
    check(st["disabled"] and "OFF after" in (vhp._captcha_blocked(P) or ""),
          "2nd failure -> breaker disabled")

    vhp._captcha_reset(P)
    check(vhp._captcha_blocked(P) is None, "reset -> re-armed")

    # Fail-safe: a corrupt/partial state file BLOCKS (never silently re-enables).
    with open(path, "w") as fh:
        fh.write("{not json")
    check("unreadable" in (vhp._captcha_blocked(P) or "").lower(),
          "corrupt state file -> BLOCKED (fail-safe)")
    vhp._captcha_reset(P)
    check(vhp._captcha_blocked(P) is None, "reset overwrites corrupt file -> re-armed")

    # Write failure -> record_failure reports not-persisted (caller must not submit).
    real_replace = vhp.os.replace

    def boom(a, b):
        raise OSError("disk full")

    vhp.os.replace = boom
    try:
        st, persisted = vhp._captcha_record_failure(P)
        check(persisted is False, "write failure -> persisted=False (abort submit)")
        check(not os.path.exists(path + ".tmp"), "failed write leaves no partial .tmp")
    finally:
        vhp.os.replace = real_replace

    # Per-property isolation.
    vhp._captcha_reset(P)
    vhp._captcha_reset(Q)
    vhp._captcha_record_failure(P)
    time.sleep(0.01)
    vhp._captcha_record_failure(P)
    check("OFF after" in (vhp._captcha_blocked(P) or ""), "bkds disabled")
    check(vhp._captcha_blocked(Q) is None, "bkdu independent of bkds")

    # Kill switches.
    open(config.AUTO_CAPTCHA_KILL_FILE, "w").close()
    check("off" in (vhp._captcha_blocked(Q) or "").lower(), "kill-file -> off")
    os.remove(config.AUTO_CAPTCHA_KILL_FILE)
    os.environ["VHP_NO_AUTO_CAPTCHA"] = "1"
    check("off" in (vhp._captcha_blocked(Q) or "").lower(), "env var -> off")
    os.environ.pop("VHP_NO_AUTO_CAPTCHA")

    # Never auto-submit a login page we merely found (recovery must be True).
    check(vhp.attempt_auto_login(object(), Q, recovery=False) is False,
          "recovery=False -> returns False (never submits a found login page)")

    # OCR precision gate: only a >=2 consensus returns a value.
    check(captcha._consensus(["123456", "123456", "999999"], min_votes=2) == "123456",
          "consensus: 2 agree (min_votes=2) -> value")
    check(captcha._consensus(["111111", "222222", "333333"], min_votes=2) is None,
          "consensus: all differ -> None")
    check(captcha._consensus(["123456"], min_votes=2) is None,
          "consensus: single read -> None")

    passed = sum(results)
    print("\n{}/{} checks passed".format(passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
