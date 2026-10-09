@echo off
REM Opens the Blue Karma VILLAGE (bkv) Chrome window for the scraper to drive.
REM 1) Log into VHP in this window by hand (solve the CAPTCHA).
REM 2) Leave the window open. The scraper attaches to it automatically.
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9224 --user-data-dir="C:\vhp-scraper\profiles\bkv" "https://e1-vhp.com/login"
