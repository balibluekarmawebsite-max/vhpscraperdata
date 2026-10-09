"""
set_login.py — save a property's VHP login into credentials.py (no Notepad).

Usage:
    C:\\Python38\\python.exe set_login.py bkv

It asks for the username and password (the password is hidden while you type),
then writes/updates credentials.py in the correct format — handling quotes,
backslashes, etc. automatically. credentials.py is git-ignored.
"""
import getpass
import os
import sys

CRED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.py")
CODES = ("bkds", "bkdu", "bkv")


def load_existing():
    """Return the existing CREDENTIALS dict, or {} if none/unreadable."""
    try:
        namespace = {}
        with open(CRED_FILE, "r") as fh:
            exec(fh.read(), namespace)  # noqa: S102 - our own local config file
        creds = namespace.get("CREDENTIALS", {})
        return creds if isinstance(creds, dict) else {}
    except Exception:  # noqa: BLE001 - missing or malformed -> start fresh
        return {}


def write(creds):
    lines = [
        "# credentials.py — VHP logins. Git-ignored; never committed.",
        "# Edit with:  python set_login.py <bkds|bkdu|bkv>",
        "CREDENTIALS = {",
    ]
    for code in CODES:
        entry = creds.get(code, {})
        lines.append(
            "    {!r}: {{'username': {!r}, 'password': {!r}}},".format(
                code, entry.get("username", ""), entry.get("password", "")
            )
        )
    lines.append("}")
    with open(CRED_FILE, "w") as fh:
        fh.write("\n".join(lines) + "\n")


def main(argv):
    if not argv or argv[0].strip().lower() not in CODES:
        print("Usage: python set_login.py <bkds|bkdu|bkv>")
        return 2
    code = argv[0].strip().lower()

    creds = load_existing()
    print("Enter the VHP login for {}.".format(code))
    user = input("  username: ").strip()
    pwd = getpass.getpass("  password (hidden, just type and press Enter): ")

    if not user or not pwd:
        print("Both username and password are required — nothing saved.")
        return 1

    creds[code] = {"username": user, "password": pwd}
    write(creds)
    print("\nSaved {} login to credentials.py.".format(code))
    print("username set: {} | password set: {}".format(bool(user), bool(pwd)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
