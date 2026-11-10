@echo off
REM ===========================================================================
REM Weekly VHP run (FRIDAY ~07:00) — for all properties:
REM   * reservation-by-creation-date (dashboard section C), and
REM   * yearly-forecast-of-room-occupancy (dashboard section B, full calendar year,
REM     net of Compliment/House Use, with revenue).
REM Running on Friday makes the reservation window (last Fri..this Thu) and the
REM dashboard week line up.
REM
REM PRE-REQS (must be true when this runs):
REM   * The 3 Chrome windows are OPEN and LOGGED INTO VHP.
REM   * IMPORT_API_TOKEN is set for this user (setx IMPORT_API_TOKEN "...").
REM
REM Output prints to the screen AND main.py writes a timestamped log under logs\.
REM ===========================================================================
cd /d C:\vhp-scraper
C:\Python38\python.exe main.py weekly
REM Capture python's exit code BEFORE the echo (echo would reset ERRORLEVEL to 0,
REM which made Task Scheduler always show "0 (OK)" even when the run failed).
set RC=%ERRORLEVEL%
echo.
echo ----- finished (exit code %RC%). Full log is under C:\vhp-scraper\logs\ -----
exit /b %RC%
