@echo off
REM ===========================================================================
REM Daily VHP run — the yearly-forecast report (dashboard section B) for all
REM properties, scraped and pushed to the dashboard. Schedule ~06:00 every day.
REM
REM PRE-REQS (must be true when this runs):
REM   * The 3 Chrome windows are OPEN and LOGGED INTO VHP
REM     (open-bkds.bat / open-bkdu.bat / open-bkv.bat, logged in by hand).
REM   * IMPORT_API_TOKEN is set for this user (setx IMPORT_API_TOKEN "...").
REM Output is appended to logs\run-daily.log; main.py also writes a timestamped
REM log under logs\.
REM ===========================================================================
cd /d C:\vhp-scraper
if not exist logs mkdir logs
echo. >> logs\run-daily.log
echo ===== %DATE% %TIME% : main.py daily ===== >> logs\run-daily.log
C:\Python38\python.exe main.py daily >> logs\run-daily.log 2>&1
