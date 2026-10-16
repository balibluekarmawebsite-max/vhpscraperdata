@echo off
REM ===========================================================================
REM Health check — prints where the VHP -> dashboard chain is (or isn't) working:
REM token, dashboard reachability, whether the scheduled tasks ran, whether the 3
REM VHP windows are logged in, and what the last runs logged. Read-only.
REM Double-click it, then send the whole window to Claude.
REM ===========================================================================
cd /d C:\vhp-scraper
C:\Python38\python.exe selfcheck.py
echo.
pause
