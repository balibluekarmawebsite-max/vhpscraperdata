"""
pullflag.py — a lightweight "a pull/re-pull is in progress" marker.

The keep-alive (keepalive.py) hard-refreshes each VHP window every few minutes.
While the poller (poller.py) is working through dashboard re-pull jobs, that
refresh must NOT happen — it would navigate a window the poller is mid-scrape and
compete for the browser. The run lock (runlock.py) already stops the two from
driving Chrome at the SAME instant, but a BURST of re-pull jobs (e.g. "re-pull all
feeds for a property") releases and re-takes the lock between feeds, and a
keep-alive tick could slip into that gap — or make a just-arrived pull wait out a
full 3-window refresh.

So the poller raises this flag the moment it claims a job and keeps it fresh until
its queue drains; the keep-alive checks the flag FIRST and skips its whole tick
while a pull is active, resuming once the poller is idle again. This is exactly
"stop the home-URL refresh while a pull is working, continue after there is no
task left."

The flag is freshness-based: if the poller dies mid-job the flag goes stale after
PULL_ACTIVE_STALE seconds and the keep-alive resumes on its own (a dead poller
isn't pulling anything, so resuming is correct). A healthy poller refreshes it
between jobs, so a long burst never looks stale.
"""
import os
import time

import config

_PATH = getattr(config, "PULL_ACTIVE_PATH", None) or (config.RUN_LOCK_PATH + ".pull")
_STALE = getattr(config, "PULL_ACTIVE_STALE", 20 * 60)


def raise_flag():
    """Mark a pull as in progress. Call on every claim and between units of work so
    it stays fresh for the whole burst. Rewriting the file bumps its mtime, which is
    what the freshness check reads. Best-effort — never breaks the poller."""
    try:
        with open(_PATH, "w") as fh:
            fh.write(str(int(time.time())))
    except OSError:
        pass


def clear_flag():
    """Pull work is done (queue drained / poller exiting) — remove the flag so the
    keep-alive can resume. Best-effort; a no-op if it's already gone."""
    try:
        os.remove(_PATH)
    except OSError:
        pass


def is_active():
    """True if a pull is in progress right now: the flag exists and is fresh. A
    stale flag (older than PULL_ACTIVE_STALE — the poller that set it is gone) is
    treated as 'not active' and removed so it doesn't linger."""
    try:
        age = time.time() - os.path.getmtime(_PATH)
    except OSError:
        return False  # no flag -> no pull in progress
    if age <= _STALE:
        return True
    clear_flag()  # stale: poller died without clearing it — let keep-alive resume
    return False
