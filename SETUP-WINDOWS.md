# Windows 7 Setup — step by step (beginner friendly)

This is the complete walkthrough for setting up the VHP scraper on your office
Windows 7 PC. It's written assuming:

- ✅ You are an **Administrator** on the PC.
- ✅ **Chrome 109** is already installed (you have `109.0.5414.120`).
- ⬜ **Python is not installed yet.**
- You'll get the code by **downloading a ZIP from GitHub**.

> **Golden rule:** Never click "Update" on Chrome or Python. Windows 7 is
> end-of-life, and the newest versions **will not run on it**. Everything here
> is deliberately an older, frozen version.

Work through the 6 stages **in order**. Each stage ends with a
**✓ How to check it worked** box. Don't move on until that check passes.

```
Stage 1  Lock Chrome so it can't auto-update past 109   (do this first!)
Stage 2  Install Python 3.8.10
Stage 3  Download the project code to C:\vhp-scraper
Stage 4  Add ChromeDriver 109 to the project
Stage 5  Install the Python packages
Stage 6  First test by hand, then schedule it
```

Stages 1–5 are a one-time setup. Stage 6 is how you actually run it.

---

## A note before you start: the Command Prompt

Several steps ask you to use the **Command Prompt** (a black window where you
type commands). To open it:

1. Click the **Start** button (round Windows button, bottom-left).
2. Type `cmd`
3. Press **Enter**. A black window opens — that's the Command Prompt.

To open it **as administrator** (needed in Stage 1):

1. Click **Start**, type `cmd`
2. **Right-click** the `cmd` / "Command Prompt" result → **Run as administrator**
3. Click **Yes** on the prompt. The window title will say "Administrator".

**Tip:** In the Command Prompt, **Ctrl+V often doesn't work** — instead
**right-click inside the window** to paste. To run a command: paste it, press
**Enter**.

---

## Stage 1 — Lock Chrome 109 so it can't auto-update

Chrome tries to update itself in the background. If it jumps to version 110 or
higher, it will **stop working on Windows 7** and break the scraper. We block
that three ways at once (any one alone isn't enough).

### 1a. Confirm your Chrome version first

Open Chrome. In the address bar at the top, type this and press Enter:

```
chrome://version
```

The first line should start with **`Google Chrome  109.0.5414.120`** (or another
`109.x`). Good.

> ⚠️ Do **not** check your version via **Menu → Help → About Google Chrome** —
> opening that page makes Chrome phone home and try to update. Always use
> `chrome://version` instead.

### 1b. Block the updates

Open a Command Prompt **as administrator** (see the note above). Paste these
**four** lines one at a time, pressing Enter after each (right-click to paste):

```cmd
reg add "HKLM\SOFTWARE\Policies\Google\Update" /v UpdateDefault /t REG_DWORD /d 0 /f
reg add "HKLM\SOFTWARE\Policies\Google\Update" /v "Update{8A69D345-D564-463C-AFF1-A69D9E530F96}" /t REG_DWORD /d 0 /f
reg add "HKLM\SOFTWARE\Policies\Google\Update" /v AutoUpdateCheckPeriodMinutes /t REG_DWORD /d 0 /f
reg add "HKLM\SOFTWARE\Policies\Google\Update" /v DisableAutoUpdateChecksCheckboxValue /t REG_DWORD /d 1 /f
```

(These set Chrome's update policy to "disabled by administrator". The long
`{8A69...}` code is Chrome's own ID.)

Now disable the two Google updater **services** — paste these four lines:

```cmd
sc config gupdate start= disabled
sc config gupdatem start= disabled
sc stop gupdate
sc stop gupdatem
```

(The space after `start=` is required. If `sc stop` says the service isn't
started, that's fine.)

Finally disable the two Google updater **scheduled tasks**:

```cmd
schtasks /Change /TN "GoogleUpdateTaskMachineCore" /DISABLE
schtasks /Change /TN "GoogleUpdateTaskMachineUA" /DISABLE
```

(If one says "cannot find the file specified", that task doesn't exist on your
PC — that's fine, move on.)

### ✓ How to check it worked

- In Chrome, go to `chrome://policy` and press Enter. You should see
  **`UpdateDefault`** listed with value **`0`**.
- Go to `chrome://version` — still shows `109.0.5414.120`.
- Leave the PC on for a day, re-check `chrome://version` — the number must not
  have changed.

---

## Stage 2 — Install Python 3.8.10

Python 3.8.10 (from May 2021) is the **last** Python that installs and runs on
Windows 7. Do not use any newer Python.

### 2a. Download the installer

On the Windows 7 PC, open **Chrome** (not the old Internet Explorer) and go to:

```
https://www.python.org/ftp/python/3.8.10/python-3.8.10-amd64.exe
```

It downloads a file named **`python-3.8.10-amd64.exe`** (about 28 MB). The
`-amd64` means 64-bit — that's the one you want.

> If that link ever fails to load, go to the official page
> <https://www.python.org/downloads/release/python-3810/> and click
> **"Windows installer (64-bit)"**.

### 2b. Run the installer — two settings matter

1. Double-click **`python-3.8.10-amd64.exe`**. If asked "allow this app to make
   changes?", click **Yes**.
2. On the **first screen**, at the **bottom**, **tick the box**
   **`Add Python 3.8 to PATH`**. *(This is the #1 thing beginners forget — it
   lets you type `python` later.)*
3. Click **`Customize installation`** (not "Install Now").
4. On the "Optional Features" screen, leave everything ticked → **Next**.
5. On the "Advanced Options" screen:
   - **Tick `Install for all users`**.
   - In the install location box, type exactly: **`C:\Python38`**
   - Click **Install**. Click **Yes** if prompted.
6. When it says "Setup was successful", click **Close**.

### ✓ How to check it worked

Open a **new** Command Prompt (close any old one first), type and Enter:

```cmd
C:\Python38\python.exe --version
```

It should print **`Python 3.8.10`**.

> **If Python won't start** (an error about a missing file
> `api-ms-win-crt-runtime-l1-1-0.dll`): your Windows is missing an update called
> **KB2999226** (the "Universal C Runtime"). Get it from
> <https://www.microsoft.com/en-us/download/details.aspx?id=49093> (file
> `Windows6.1-KB2999226-x64.msu`), double-click it, click Yes, reboot, then try
> again. Most updated Win7 PCs already have it.

---

## Stage 3 — Download the project code

### 3a. Download the ZIP from GitHub

1. On the Windows 7 PC, open Chrome and go to your repository:
   <https://github.com/balibluekarmawebsite-max/vhpscraperdata>
2. Near the top of the branch list, make sure the branch
   **`claude/bold-feynman-gv3gnl`** is selected (click the branch dropdown that
   says `main`/`master` and pick it) — that's where the code lives right now.
3. Click the green **`<> Code`** button → **`Download ZIP`**.
4. It saves a file like `vhpscraperdata-claude-bold-feynman-gv3gnl.zip` to your
   Downloads folder.

### 3b. Unzip it to C:\vhp-scraper

1. In the Downloads folder, **right-click** the ZIP → **`Extract All…`** →
   **Extract**.
2. Open the extracted folder. You'll see the project files inside (possibly one
   folder deep, named like `vhpscraperdata-...`). The files `main.py`,
   `config.py`, `README.md` etc. should be **directly** inside.
3. Create a folder **`C:\vhp-scraper`** and copy all those files into it, so you
   end up with **`C:\vhp-scraper\main.py`**, `C:\vhp-scraper\config.py`, etc.

> The path **`C:\vhp-scraper`** matters — it's written into `run.bat`. If you
> put the project somewhere else, you'll later edit the two paths inside
> `run.bat` to match.

### ✓ How to check it worked

Open a Command Prompt and run:

```cmd
cd /d C:\vhp-scraper
dir
```

You should see `main.py`, `config.py`, `browser.py`, `requirements.txt`, etc.
listed. (The `cd /d` command moves you into the folder.)

---

## Stage 4 — Add ChromeDriver 109

ChromeDriver is a little helper program Selenium uses to control Chrome. Its
version must match Chrome's major version (**109**).

### 4a. Download it

On the Windows 7 PC, open Chrome and go to this exact link (verified working):

```
https://chromedriver.storage.googleapis.com/109.0.5414.74/chromedriver_win32.zip
```

It downloads **`chromedriver_win32.zip`** (about 7 MB).

> - Note the **underscore** in `chromedriver_win32.zip` (not a hyphen).
> - "win32" is correct even on 64-bit Windows — for Chrome 109 there's only a
>   32-bit build, and it works fine.
> - `109.0.5414.74` is the right driver for your `109.0.5414.120` Chrome — only
>   the first number (`109`) has to match.

### 4b. Unzip and place it

1. Right-click `chromedriver_win32.zip` → **Extract All…** → **Extract**.
2. Inside is a single file: **`chromedriver.exe`**.
3. Copy `chromedriver.exe` into **`C:\vhp-scraper`** — the same folder as
   `main.py`. Final location: **`C:\vhp-scraper\chromedriver.exe`**.

### ✓ How to check it worked

```cmd
cd /d C:\vhp-scraper
chromedriver.exe --version
```

It should print a line starting with **`ChromeDriver 109.0.5414.74`**.

> If Windows blocked it ("came from another computer"): right-click
> `chromedriver.exe` → **Properties** → tick **Unblock** → OK.

---

## Stage 5 — Install the Python packages

This installs the exact pinned versions the project needs (`selenium 4.9.1`,
`requests 2.31.0`). Do **not** add `--upgrade` — the old versions are
intentional.

Open a Command Prompt and run these two lines (Enter after each):

```cmd
cd /d C:\vhp-scraper
C:\Python38\python.exe -m pip install -r requirements.txt
```

It'll download and install for a minute or two.

### ✓ How to check it worked

```cmd
C:\Python38\python.exe -c "import selenium, requests; print(selenium.__version__, requests.__version__)"
```

It should print exactly: **`4.9.1 2.31.0`**

> **If you get an SSL / certificate error** (common on old, un-updated Win7),
> try refreshing the certificate bundle first:
> `C:\Python38\python.exe -m pip install --upgrade certifi`
> then re-run the install. If it still fails, add this to the install command
> (only on a network you trust):
> `--trusted-host pypi.org --trusted-host files.pythonhosted.org`
>
> **If pip tries to upgrade itself and breaks:** don't let pip go past 25.0.1 on
> Python 3.8. If needed: `C:\Python38\python.exe -m pip install --upgrade "pip<25.1"`
> (keep the quotes).

---

## Stage 6 — First test, then schedule it

> ⚠️ **Before this stage will do anything useful**, the project still needs its
> real VHP details filled in — see **"What's left after setup"** at the bottom.
> But you can still run Stage 6a now to confirm the plumbing (Python + Chrome +
> driver) all talk to each other.

### 6a. Log in once per property

This opens Chrome so you can log into VHP by hand; the login is then saved.

```cmd
cd /d C:\vhp-scraper
C:\Python38\python.exe login.py bkds
```

A Chrome 109 window should open. **If it opens without a driver error, the whole
stack works** — that's the milestone for setup. (It will complain that the VHP
URL is still a placeholder until you fill in `config.py` — that's expected for
now.) Repeat later for `bkdu` and `bkv` once the URLs are set.

### 6b. Run it by hand

Once `config.py` is filled in (next phase), test a real run:

```cmd
cd /d C:\vhp-scraper
run.bat daily
```

Watch Chrome drive itself, then check the `C:\vhp-scraper\logs` folder for a new
log file.

### 6c. Schedule two tasks (Daily + Weekly)

Only do this once a hand-run works. Open **Task Scheduler** (Start → type
`Task Scheduler` → Enter).

1. On the right, click **`Create Task…`** — **NOT** "Create Basic Task…".
   (The Basic one hides the setting we need.)
2. **General tab:**
   - Name: `VHP Scraper - Daily`
   - Select **`Run only when user is logged on`** ← **the most important
     setting.** (The browser is visible, so it needs a logged-in desktop. The
     "whether logged on or not" option runs it hidden and breaks it.)
   - Leave "Run with highest privileges" **unticked**.
   - "Configure for:" → **Windows 7**.
3. **Triggers tab** → **New…** → "On a schedule" → **Daily** → time **06:00:00**
   → OK.
4. **Actions tab** → **New…** → "Start a program":
   - Program/script: `C:\vhp-scraper\run.bat`
   - Add arguments: `daily`
   - **Start in:** `C:\vhp-scraper`  ← **no quotation marks** around it, or the
     task fails with error `0x8007010B`.
   - OK.
5. **Conditions tab:** untick "Start the task only if the computer is on AC
   power".
6. **Settings tab:** tick "Run task as soon as possible after a scheduled start
   is missed"; set "Stop the task if it runs longer than 1 hour". Click **OK** to
   save.
7. **Repeat** steps 1–6 for the **Weekly** task, with only these differences:
   name `VHP Scraper - Weekly`; trigger **Weekly**, **Monday**, time
   **06:30:00**; Add arguments **`weekly`**.

Keep the PC **powered on and logged in** (you may **lock** the screen with
**Win+L**, but do **not** sign out). Set Power Options → "Put the computer to
sleep" → **Never**.

### ✓ How to check it worked

In Task Scheduler, select **`VHP Scraper - Daily`** → click **Run** (right
pane). Chrome should open and drive itself. When done, the task's **Last Run
Result** should show **`0x0`** (success). Check `C:\vhp-scraper\logs` for a
fresh log.

---

## What's left after setup (the next phase)

Setup gets the *machine* ready. Before real data flows, the project needs **your
VHP details** — this is what we do together next:

1. **Edit `config.py`** — paste the 3 real VHP URLs, list your exact reports,
   confirm the dashboard upload endpoint.
2. **Record the report navigation** in `vhp.py` (which menus/buttons to click to
   reach each report and export it).
3. Log in for real (`login.py bkds` / `bkdu` / `bkv`), then test `run.bat daily`.

The big thing to check as soon as Chrome opens on VHP: **is the report data
real HTML, or one big picture/remote screen?** (Right-click a data table →
Inspect.) That decides whether this Selenium approach works as-is. See the
"Open questions" section in `README.md`.

---

## Quick reference — all the checks in one place

| Stage | Command | Expect |
|---|---|---|
| 1 | `chrome://version` (in Chrome) | `109.0.5414.120` |
| 2 | `C:\Python38\python.exe --version` | `Python 3.8.10` |
| 3 | `cd /d C:\vhp-scraper` then `dir` | sees `main.py` |
| 4 | `chromedriver.exe --version` | `ChromeDriver 109.0.5414.74` |
| 5 | `...python.exe -c "import selenium,requests;print(selenium.__version__,requests.__version__)"` | `4.9.1 2.31.0` |
| 6 | `run.bat daily` | Chrome opens, log file appears |
