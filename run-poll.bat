@echo off
REM ===========================================================================
REM On-demand VHP poller — watches the dashboard for scrape requests and runs
REM them immediately. Leave this running all day (double-click it, or start it
REM at logon with a scheduled task). It polls quietly when there's nothing to do.
REM
REM PRE-REQS (same as the scheduled runs):
REM   * The 3 Chrome windows are OPEN and LOGGED INTO VHP
REM     (open-bkds.bat / open-bkdu.bat / open-bkv.bat, logged in by hand).
REM   * IMPORT_API_TOKEN is set for this user (setx IMPORT_API_TOKEN "...").
REM
REM It coordinates with the scheduled daily/weekly runs automatically (a lock
REM file makes sure only one of them drives Chrome at a time), so it's safe to
REM leave running even at 06:00 / Friday 07:00.
REM
REM Output prints to this window AND is saved under C:\vhp-scraper\logs\poller-*.log.
REM Close this window (or Ctrl-C) to stop it.
REM ===========================================================================
cd /d C:\vhp-scraper
echo Starting VHP on-demand poller. Leave this window open. Close it to stop.
C:\Python38\python.exe poller.py
echo.
echo ----- poller stopped (exit code %ERRORLEVEL%). Log is under C:\vhp-scraper\logs\ -----
pause
