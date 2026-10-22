"""
check_token.py — safely inspect the dashboard API token the scraper will send,
to debug HTTP 401 "invalid token". It does NOT print the whole secret — only its
length and a masked preview (first 3 + last 3 chars), plus warnings about hidden
characters (BOM / quotes / extra lines) that silently break auth.

    C:\\Python38\\python.exe check_token.py

Compare the length + masked preview it prints against the token your dashboard
expects. If they differ, the file has the wrong token (re-save it). If they match
but you still get 401, the problem is on the dashboard side (token not deployed,
or that endpoint expects a different one).
"""
import os

import config


def _mask(t):
    if not t:
        return "(EMPTY)"
    if len(t) <= 8:
        return "*" * len(t) + " (len {})".format(len(t))
    return "{}...{}  (len {})".format(t[:3], t[-3:], len(t))


def main():
    env = os.environ.get("IMPORT_API_TOKEN", "")
    print("Resolved token the scraper will send:")
    print("   value : {}".format(_mask(config.IMPORT_API_TOKEN)))
    print("   source: {}".format("env IMPORT_API_TOKEN" if env else "token file"))
    print()

    for name in ("import_token.txt", ".import_token"):
        p = os.path.join(config.BASE, name)
        if not os.path.isfile(p):
            continue
        with open(p, "rb") as fh:
            raw = fh.read()
        print("File: {}".format(p))
        print("   size (bytes)          : {}".format(len(raw)))
        print("   has UTF-8 BOM         : {}{}".format(
            raw[:3] == b"\xef\xbb\xbf",
            "   <-- a BOM corrupts the token; re-save without it" if raw[:3] == b"\xef\xbb\xbf" else ""))
        print("   leading/trailing space: {}".format(raw != raw.strip()))
        stripped = raw.strip()
        print("   wrapped in quotes     : {}".format(
            stripped[:1] in (b'"', b"'")))
        print("   multiple lines        : {}".format(b"\n" in stripped))
        print("   (these are auto-cleaned now, but a clean file is safest)")
        print()

    print("To (re)write the file cleanly on Windows — no newline, no BOM, no quotes:")
    print(r'   C:\Python38\python.exe -c "open(r' + "'"
          + os.path.join(config.BASE, "import_token.txt")
          + "','w').write('PASTE_TOKEN_HERE')\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
