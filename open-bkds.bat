@echo off
REM Opens the Blue Karma Dijiwa SEMINYAK (bkds) Chrome window for the scraper.
REM 1) Log into VHP in this window by hand (solve the CAPTCHA).
REM 2) Leave the window open. The scraper attaches to it automatically.
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\vhp-scraper\profiles\bkds" "https://e1-vhp.com/login"
