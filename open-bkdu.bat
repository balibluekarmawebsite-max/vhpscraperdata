@echo off
REM Opens the Blue Karma Dijiwa UBUD (bkdu) Chrome window for the scraper.
REM 1) Log into VHP in this window by hand (solve the CAPTCHA).
REM 2) Leave the window open. The scraper attaches to it automatically.
REM The extra flags just trim background memory (no extensions, no background
REM networking, no auto-update) so the window is less likely to hit Chrome's
REM "Aw, Snap! Out of Memory" over a long day. They don't change how VHP renders
REM and don't log you out (same profile folder = same saved session).
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9223 --user-data-dir="C:\vhp-scraper\profiles\bkdu" --disable-extensions --disable-background-networking --disable-component-update "https://e1-vhp.com/login"
