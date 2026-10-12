@echo off
REM ===========================================================================
REM Daily VHP run — the yearly-forecast report (dashboard section B) for all
REM properties, scraped and pushed to the dashboard. Schedule ~06:00 every day.
REM
REM PRE-REQS (must be true when this runs):
REM   * The 3 Chrome windows are OPEN and LOGGED INTO VHP
REM     (open-bkds.bat / open-bkdu.bat / open-bkv.bat, logged in by hand).
REM   * IMPORT_API_TOKEN is set for this user (setx IMPORT_API_TOKEN "...").
REM
REM Output prints to the screen AND main.py writes a timestamped log under logs\.
REM ===========================================================================
cd /d C:\vhp-scraper
C:\Python38\python.exe main.py daily
echo.
echo ----- finished (exit code %ERRORLEVEL%). Full log is under C:\vhp-scraper\logs\ -----
