"""
runlock.py — a simple cross-process lock so only ONE VHP run touches the Chrome
windows at a time.

Why this exists: the scraper attaches to the user's open Chrome windows and
starts a chromedriver per property. If two runs overlap — e.g. the scheduled
6am/Friday run (main.py) and an on-demand job (poller.py) — the second run's
kill_stale_chromedrivers() would kill the first run's driver mid-scrape (the
exact "connection refused" clash we hit when running properties concurrently).

The lock is a single file created atomically (O_CREAT | O_EXCL). Whoever creates
it holds the lock; everyone else waits or skips. A lock older than `stale_after`
seconds is treated as dead (a crashed run that never released it) and stolen, so
a crash can never wedge the machine permanently.

Works the same on Windows 7 / Python 3.8 and on Linux (O_EXCL is atomic on both).
"""
import errno
import os
import time


class LockBusy(Exception):
    """Raised by acquire(wait=...) when the lock is held by a live run and the
    wait budget ran out."""


class RunLock(object):
    """Usage:
        lock = RunLock(path)
        if lock.acquire(wait=0):      # try once, don't block
            try:
                ... do the work ...
            finally:
                lock.release()

    Or as a context manager (raises LockBusy if it can't get it in `wait`):
        with RunLock(path).hold(wait=900):
            ... do the work ...
    """

    def __init__(self, path, stale_after=1800):
        self.path = path
        self.stale_after = stale_after
        self.fd = None

    # -- low level -----------------------------------------------------------
    def _try_create(self):
        """Atomically create the lock file. Returns True on success, False if it
        already exists. Any other OS error propagates."""
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                return False
            raise
        self.fd = fd
        try:
            os.write(fd, "{} {}".format(os.getpid(), int(time.time())).encode("ascii"))
        except OSError:
            pass
        return True

    def _is_stale(self):
        """True if the existing lock file is older than stale_after (a dead run)."""
        try:
            age = time.time() - os.path.getmtime(self.path)
        except OSError:
            return True  # vanished between checks — treat as free
        return age > self.stale_after

    def _steal(self):
        try:
            os.remove(self.path)
        except OSError:
            pass

    # -- public --------------------------------------------------------------
    def acquire(self, wait=0, poll=2.0):
        """Take the lock. `wait` = max seconds to keep trying before giving up
        (0 = a single attempt). Returns True if acquired, False if `wait` elapsed
        while a LIVE run held it. A stale (dead) lock is always stolen."""
        deadline = time.time() + max(0, wait)
        while True:
            if self._try_create():
                return True
            # Already locked. If the holder looks dead, steal it and retry now.
            if self._is_stale():
                self._steal()
                continue
            if time.time() >= deadline:
                return False
            time.sleep(poll)

    def refresh(self):
        """Bump the lock's mtime so a long but healthy run isn't judged stale.
        Call between units of work (e.g. after each report)."""
        try:
            os.utime(self.path, None)
        except OSError:
            pass

    def release(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        try:
            os.remove(self.path)
        except OSError:
            pass

    # -- context-manager form (raises if it can't get it) --------------------
    def hold(self, wait=0, poll=2.0):
        self._hold_wait = wait
        self._hold_poll = poll
        return self

    def __enter__(self):
        if not self.acquire(wait=getattr(self, "_hold_wait", 0),
                            poll=getattr(self, "_hold_poll", 2.0)):
            raise LockBusy("another VHP run holds the lock ({})".format(self.path))
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False
