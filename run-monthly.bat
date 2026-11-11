@echo off
REM ===========================================================================
REM Monthly VHP run — the SALES feeds for all properties:
REM   * Account Production (YTD) -> /api/sales/account-production  (Sales Highlight)
REM   * Room Types (actuals)     -> /api/sales/room-types          (Rooms Analytics)
REM Schedule for the **1st of every month at 08:00** — AFTER the 06:00 daily and
REM 07:00 Friday weekly runs, so it never clashes (the run-lock also prevents overlap).
REM
REM It reports the PREVIOUS completed month, YTD. Examples:
REM   * run on 1 Dec 2026 -> Date range 1 Jan .. 30 Nov 2026  (period 2026-11)
REM   * run on 1 Jan 2027 -> Date range 1 Jan .. 31 Dec 2026  (period 2026-12)
REM
REM PRE-REQS (must be true when this runs):
REM   * The 3 Chrome windows are OPEN and LOGGED INTO VHP
REM     (open-bkds.bat / open-bkdu.bat / open-bkv.bat, logged in by hand).
REM   * IMPORT_API_TOKEN is set for this user (setx IMPORT_API_TOKEN "...").
REM
REM Output is appended to C:\vhp-scraper\logs\monthly.log (it only runs ~once a
REM month, so the file stays small). To watch it live instead, run by hand:
REM   python run_account_production.py all --month 2026-11
REM ===========================================================================
cd /d C:\vhp-scraper
if not exist logs mkdir logs
REM Auto-update to the latest committed code first (fast-forward only, non-fatal):
REM a scheduled run never uses a stale checkout. If it can't pull, it carries on.
git pull --ff-only
echo. >> logs\monthly.log
echo ===== [%date% %time%] account-production monthly run starting ===== >> logs\monthly.log
C:\Python38\python.exe run_monthly.py all --post >> logs\monthly.log 2>&1
set RC=%ERRORLEVEL%
echo ===== [%date% %time%] finished (exit code %RC%) ===== >> logs\monthly.log
exit /b %RC%
