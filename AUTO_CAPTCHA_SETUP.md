# Auto-login after a VHP logout (CAPTCHA auto-read)

When VHP logs out, the bot can now try to log back in **by itself** instead of only
alerting you. It clicks **OK** on the "session expired" box, then reads the numeric
CAPTCHA and signs in.

> ⚠️ **You opted into this knowing the risk.** The CAPTCHA exists to stop bots.
> Reading it is never 100% reliable, and **enough wrong guesses can get the VHP
> account locked.** So the bot is built to be *un-hammerable* (see Safeguards) and
> to hand back to a human the moment it's unsure. Test the accuracy first (Step 3)
> and keep the OFF switch handy (last section).

---

## What happens on a logout now

1. Bot sees the **"Your Session has been expired"** box → clicks **OK**
   (this alone is safe — it just closes the box, no reload).
2. On the login page, if auto-CAPTCHA is on and allowed right now, it:
   - leaves your remembered username/password in place (fills them from
     `credentials.py` only if the boxes are empty),
   - **reads the CAPTCHA** — and only if several reads **agree** does it type it
     and click **LOG IN** (one attempt),
   - if it can't read it confidently, it does **nothing** and leaves the window
     ready for you to type it.
3. If the login **fails**, it hands back to a human and won't keep trying
   (see Safeguards).

If auto-CAPTCHA is off or not installed, you get the old behaviour: the window is
left ready on the login page and you're alerted to type the CAPTCHA yourself.

---

## Step 1 — Install the OCR engine on the PC (one time)

```
C:\Python38\python.exe -m pip install pytesseract pillow
```

Then install the **Tesseract-OCR engine** (the thing that actually reads the
image) — download the Windows installer for Tesseract and run it. If, after
installing, the bot still says "OCR not installed", point it at the program:
set an environment variable `TESSERACT_CMD` to the full path, e.g.

```
setx TESSERACT_CMD "C:\Program Files\Tesseract-OCR\tesseract.exe"
```

(If you skip this step, nothing breaks — the bot just keeps asking a human to type
the CAPTCHA, exactly like before.)

## Step 2 — (Optional) store the VHP username/password

The browser already remembers them in each window, so this is only a backup in
case a profile ever forgets. It's git-ignored (never committed):

```
C:\Python38\python.exe set_login.py bkds
C:\Python38\python.exe set_login.py bkdu
C:\Python38\python.exe set_login.py bkv
```

## Step 3 — Test the reading accuracy BEFORE trusting it (no risk)

Get a window onto the VHP login page (so a CAPTCHA is showing), then:

```
C:\Python38\python.exe test_captcha.py bkds 10
```

This **only reads** the CAPTCHA and prints what it saw — it never submits
anything, so there's zero lockout risk. Click VHP's refresh-CAPTCHA icon between
reads. You want lots of **CONFIDENT** reads whose digits actually match the
screen. If it's mostly "not confident" or the confident ones are **wrong**, turn
auto-CAPTCHA **off** (last section) and keep typing them by hand — the OK-click and
"leave the window ready" help still work.

---

## Safeguards (why it can't lock the account)

All per property (bkds/bkdu/bkv are independent):

- **One attempt per logout.** No retry loop — it types the CAPTCHA once and checks
  the result.
- **Only submits when confident.** It needs several image reads to agree; an
  unsure read submits **nothing** (so it never "guesses").
- **Cooldown.** At least **5 minutes** between real login attempts.
- **Auto-OFF after 2 failures.** Two failed logins in a row and auto-login
  **switches itself off** for that property until a **human logs in successfully**
  (which re-arms it). So the bot can never rack up failed logins toward a lockout.
- **Backs off from humans.** If the CAPTCHA box already has text (someone's
  typing), the bot leaves it alone.
- **Never reloads a login page** you're on (so it can't wipe a CAPTCHA you're
  typing).

Tune these in `config.py`: `AUTO_CAPTCHA_COOLDOWN_SEC`, `AUTO_CAPTCHA_MAX_FAILURES`.

## What you'll see in the logs

`logs\keepalive.log` lines like:
- `auto-login: logged in OK (CAPTCHA '578241' accepted)` — it worked.
- `auto-login: CAPTCHA not read confidently — handing to a human` — you type it.
- `auto-login: login FAILED ... auto-login now OFF until a human logs in` — breaker
  tripped; log in once by hand to re-arm.

## Turn it OFF instantly (no code edit)

Either one takes effect on the next run:

- Create an empty file **`logs\.no_auto_captcha`**, **or**
- Set the environment variable **`VHP_NO_AUTO_CAPTCHA=1`**.

Delete the file / unset the variable to turn it back on. (Or set
`AUTO_CAPTCHA_ENABLED = False` in `config.py`.)
