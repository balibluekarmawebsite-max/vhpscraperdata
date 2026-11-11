@echo off
REM ===========================================================================
REM Daily VHP run — the DAILY reports for all properties: the forward PICKUP
REM occupancy (yearly-forecast-pickup -> /api/daily/pickup, current month onward,
REM net of Compliment/House Use, with revenue) plus the reservation and
REM room-departure JSON feeds. Schedule ~06:00 every day. (The full calendar-year
REM occupancy, dashboard section B, is now a WEEKLY report — see run-weekly.bat.)
REM
REM PRE-REQS (must be true when this runs):
REM   * The 3 Chrome windows are OPEN and LOGGED INTO VHP
REM     (open-bkds.bat / open-bkdu.bat / open-bkv.bat, logged in by hand).
REM   * IMPORT_API_TOKEN is set for this user (setx IMPORT_API_TOKEN "...").
REM
REM Output prints to the screen AND main.py writes a timestamped log under logs\.
REM ===========================================================================
cd /d C:\vhp-scraper
REM Auto-update to the latest committed code FIRST, so a scheduled run never uses a
REM stale checkout (which is how an old, pre-fix version kept pushing wrong data).
REM Fast-forward only + non-fatal: if it can't pull (offline / diverged / git not on
REM PATH), it prints the reason and the run carries on with the current code.
git pull --ff-only
C:\Python38\python.exe main.py daily
REM Capture python's exit code BEFORE the echo (echo would reset ERRORLEVEL to 0,
REM which made Task Scheduler always show "0 (OK)" even when the run failed).
set RC=%ERRORLEVEL%
echo.
echo ----- finished (exit code %RC%). Full log is under C:\vhp-scraper\logs\ -----
exit /b %RC%
