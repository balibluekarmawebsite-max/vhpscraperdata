@echo off
REM Opens the Blue Karma VILLAGE (bkv) Chrome window for the scraper to drive.
REM 1) Log into VHP in this window by hand (solve the CAPTCHA).
REM 2) Leave the window open. The scraper attaches to it automatically.
REM The extra flags do two things, and change nothing about how VHP renders or
REM your saved login:
REM  * trim background memory (no extensions / background networking / auto-update)
REM    so the window is less likely to hit "Aw, Snap! Out of Memory";
REM  * KEEP THE WINDOW AWAKE in the background — stop Chrome throttling, freezing
REM    or de-prioritising the tab when it's minimised or covered, which is what can
REM    make it unresponsive to the scraper/keep-alive (the last 4 flags).
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9224 --user-data-dir="C:\vhp-scraper\profiles\bkv" --disable-extensions --disable-background-networking --disable-component-update --disable-background-timer-throttling --disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-features=CalculateNativeWinOcclusion "https://e1-vhp.com/login"
