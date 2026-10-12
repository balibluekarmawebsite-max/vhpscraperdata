@echo off
REM ===========================================================================
REM Weekly VHP run — the reservation-by-creation-date report (dashboard section
REM C) for all properties. Schedule for FRIDAY ~07:00. Running on Friday makes
REM the date window (last Fri..this Thu) and the dashboard week line up.
REM
REM PRE-REQS (must be true when this runs):
REM   * The 3 Chrome windows are OPEN and LOGGED INTO VHP.
REM   * IMPORT_API_TOKEN is set for this user (setx IMPORT_API_TOKEN "...").
REM Output is appended to logs\run-weekly.log; main.py also writes a timestamped
REM log under logs\.
REM ===========================================================================
cd /d C:\vhp-scraper
if not exist logs mkdir logs
echo. >> logs\run-weekly.log
echo ===== %DATE% %TIME% : main.py weekly ===== >> logs\run-weekly.log
C:\Python38\python.exe main.py weekly >> logs\run-weekly.log 2>&1
