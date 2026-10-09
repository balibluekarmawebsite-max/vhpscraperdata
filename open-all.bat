@echo off
REM Opens all 3 property windows at once. Log into VHP in each (solve the
REM CAPTCHA) and leave them open; the scraper drives them on schedule.
call "%~dp0open-bkds.bat"
call "%~dp0open-bkdu.bat"
call "%~dp0open-bkv.bat"
