@echo off
REM ===========================================================================
REM Keep-alive — nudges the 3 VHP windows so the sessions don't time out.
REM
REM This .bat is for RUNNING IT BY HAND to watch what happens. The hourly
REM scheduled task runs it silently with pythonw.exe (no console window) — see
REM SETUP-WINDOWS.md for the schtasks command.
REM
REM Add --search after keepalive.py if plain navigation isn't keeping VHP alive.
REM ===========================================================================
cd /d C:\vhp-scraper
C:\Python38\python.exe keepalive.py
echo.
echo ----- keep-alive finished (exit code %ERRORLEVEL%). Log is under C:\vhp-scraper\logs\ -----
pause
