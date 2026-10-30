@echo off
REM ===========================================================================
REM Weekly VHP run — the reservation-by-creation-date report (dashboard section
REM C) for all properties. Schedule for FRIDAY ~07:00. Running on Friday makes
REM the date window (last Fri..this Thu) and the dashboard week line up.
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
