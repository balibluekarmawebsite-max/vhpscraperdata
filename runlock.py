"""
runlock.py — a simple cross-process lock so only ONE VHP run touches the Chrome
windows at a time.

Why this exists: the scraper attaches to the user's open Chrome windows and
starts a chromedriver per property. If two runs overlap — e.g. the scheduled
6am/Friday run (main.py), an on-demand job (poller.py), or the hourly keep-alive
(keepalive.py) — the second run's kill_stale_chromedrivers() would kill the
first run's driver mid-scrape (the "connection refused" clash we hit when running
properties concurrently).

The lock is a single file created atomically (O_CREAT | O_EXCL). Whoever creates
it holds the lock; everyone else waits or skips. Each lock carries a random
"nonce" written into the file, so a holder only ever deletes/refreshes a file
that is still ITS lock (never one a different run has since created).

Crash safety: a healthy holder calls refresh() between units of work to keep the
file's mtime current. A file whose mtime is older than `stale_after` is treated
as a dead run and stolen. On Windows an open O_EXCL file can't be removed by
another process, so a live holder can never be stolen out from under its own open
handle; refresh() keeps a long-but-healthy run from ever looking stale.

Works on Windows 7 / Python 3.8 and on Linux.
"""
import errno
import os
import time
import uuid


class LockBusy(Exception):
    """Raised by the `hold()` context manager when the lock can't be taken in
    the allotted wait."""


# Sentinel: we tried to read the lock file but couldn't (e.g. Windows keeps it
# open) — distinct from "it's gone" and from a concrete nonce value.
_UNREADABLE = object()


class RunLock(object):
    """Usage:
        lock = RunLock(path)
        if lock.acquire(wait=0):      # try once, don't block
            try:
                ... do the work, calling lock.refresh() between units ...
            finally:
                lock.release()

    Or as a context manager (raises LockBusy if it can't get it in `wait`):
        with RunLock(path).hold(wait=900):
            ...
    """

    def __init__(self, path, stale_after=1800):
        self.path = path
        self.stale_after = stale_after
        self.fd = None
        self.nonce = None

    # -- low level -----------------------------------------------------------
    def _try_create(self):
        """Atomically create the lock file and stamp our nonce into it. Returns
        True on success, False if it already exists; other OS errors propagate."""
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                return False
            raise
        self.fd = fd
        self.nonce = uuid.uuid4().hex
        try:
            os.write(fd, "{} {} {}".format(os.getpid(), int(time.time()), self.nonce).encode("ascii"))
        except OSError:
            pass
        return True

    def _file_nonce_state(self):
        """Read the nonce (3rd whitespace token) out of the lock file.
        Returns ('read', nonce_or_None), ('gone', None) if the file is missing,
        or ('unreadable', None) if it exists but we can't open it (e.g. Windows
        holding it open on our own handle)."""
        try:
            with open(self.path, "r") as fh:
                parts = fh.read().split()
            return "read", (parts[2] if len(parts) >= 3 else None)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return "gone", None
            return "unreadable", None

    def _stolen(self):
        """True only if we can POSITIVELY confirm the lock file is no longer ours
        (a different nonce, or it's gone). If we can't read it, assume it's still
        ours — on Windows it can't be stolen while our O_EXCL handle is open."""
        if self.nonce is None:
            return True
        state, val = self._file_nonce_state()
        if state == "gone":
            return True
        if state == "unreadable":
            return False
        return val != self.nonce

    def _is_stale(self):
        """True if the existing lock file's mtime is older than stale_after (a
        run that died without releasing, and without a recent refresh())."""
        try:
            age = time.time() - os.path.getmtime(self.path)
        except OSError:
            return True  # vanished between checks — treat as free
        return age > self.stale_after

    def _steal(self):
        """Remove a dead holder's lock file. Returns True if the path is now free
        (removed, or already gone), False if removal failed — which on Windows
        means the holder is still ALIVE (its open O_EXCL handle blocks removal),
        so we must NOT keep retrying in a tight loop."""
        try:
            os.remove(self.path)
            return True
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return True
            return False

    # -- public --------------------------------------------------------------
    def acquire(self, wait=0, poll=2.0):
        """Take the lock. `wait` = max seconds to keep trying before giving up
        (0 = a single attempt). Returns True if acquired, False if `wait` elapsed
        while a LIVE run held it. A stale (dead) lock is stolen and retaken."""
        deadline = time.time() + max(0, wait)
        while True:
            if self._try_create():
                return True
            # Already locked. Steal only if it looks dead AND removal succeeds;
            # a failed steal means a live holder (Windows), so fall through to
            # the wait/deadline instead of spinning.
            if self._is_stale() and self._steal():
                continue
            if time.time() >= deadline:
                return False
            time.sleep(poll)

    def refresh(self):
        """Bump the lock's mtime so a long but healthy run isn't judged stale.
        Call between units of work (after each report / before each job). No-op if
        we don't hold it or it's been stolen."""
        if self.fd is None or self._stolen():
            return
        try:
            os.utime(self.path, None)
        except OSError:
            pass

    def release(self):
        """Close our handle and remove the lock file — but only if it's still
        OURS (never delete a file a different run has since created at this path)."""
        stolen = self._stolen()  # check before closing our handle
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        if not stolen:
            try:
                os.remove(self.path)
            except OSError:
                pass
        self.nonce = None

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
