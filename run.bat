@echo off
REM run.bat — wrapper that Windows Task Scheduler calls.
REM Usage:  run.bat daily   |   run.bat weekly   |   run.bat both
REM
REM Adjust the two paths below to match this machine:
REM   - the project folder (where this file lives)
REM   - the Python 3.8 interpreter

cd /d C:\vhp-scraper
if not exist logs mkdir logs
C:\Python38\python.exe main.py %1 >> logs\run.log 2>&1
