@echo off
REM ===========================================================================
REM On-demand VHP poller — watches the dashboard for "Request scrape" jobs and
REM runs them immediately. LEAVE THIS WINDOW OPEN (close it, or Ctrl-C, to stop).
REM
REM poller.py polls forever on its own (every ~2 min, 2s between jobs). This
REM wrapper also RESTARTS it after 10s if it ever exits (e.g. a crash), so the
REM dashboard's "poller" badge stays green and every scrape you request just runs.
REM
REM PRE-REQS:
REM   * The 3 Chrome windows are OPEN and LOGGED INTO VHP
REM     (open-bkds.bat / open-bkdu.bat / open-bkv.bat, logged in by hand).
REM   * IMPORT_API_TOKEN is set for this user (setx IMPORT_API_TOKEN "...").
REM
REM IMPORTANT: this must run in YOUR logged-on Windows session, because it drives
REM the Chrome windows YOU opened. Do NOT set it to "run whether logged on or not"
REM and do NOT run it as a session-0 service (NSSM) — those can't see your Chrome
REM windows. To auto-start it, use a scheduled task with trigger "At log on" and
REM "Run only when user is logged on" (see SETUP-WINDOWS.md).
REM
REM It coordinates with the scheduled daily/weekly runs and the keep-alive via a
REM lock file, so only one of them drives Chrome at a time — safe to leave running.
REM Per-run output is also saved under C:\vhp-scraper\logs\poller-*.log.
REM ===========================================================================
cd /d C:\vhp-scraper
:loop
echo [%date% %time%] starting VHP poller (leave this window open; close to stop)...
C:\Python38\python.exe poller.py
echo [%date% %time%] poller exited (code %ERRORLEVEL%) - restarting in 10s...
timeout /t 10 /nobreak >nul
goto loop
