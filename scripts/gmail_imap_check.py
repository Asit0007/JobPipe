#!/usr/bin/env python3
"""Verify the Gmail App Password before a cron depends on it.

    make gmail-imap-check PY=./.venv/bin/python

Checks, in order, and stops at the first failure with the fix in the message:
login, mailbox open, and the alert query actually returning mail. The last one
matters most -- a credential that authenticates but matches nothing looks
identical to a working setup right up until the pipeline reports "0 from 0
emails" and calls it a success. That exact failure is CLAUDE.md 7.34.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobpipe import gmail                                    # noqa: E402
from jobpipe.config import env, env_int                      # noqa: E402


def main() -> int:
    address = (env("GMAIL_ADDRESS") or "").strip()
    raw = env("GMAIL_APP_PASSWORD") or ""

    print("Gmail IMAP preflight\n")
    if not address or not raw.strip():
        print("  FAIL  GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set in .env\n")
        print("  1. Enable 2-Step Verification : https://myaccount.google.com/security")
        print("  2. Create an App Password     : https://myaccount.google.com/apppasswords")
        print("  3. Add to .env:")
        print("       GMAIL_ADDRESS=you@gmail.com")
        print("       GMAIL_APP_PASSWORD=abcd efgh ijkl mnop")
        return 1

    # A present-but-empty value reads as "configured" to a careless grep. That
    # trap already cost a diagnosis once, on TELEGRAM_CHAT_ID.
    pw = raw.replace(" ", "").strip()
    print(f"  address   {address}")
    # A Google App Password is exactly 16 lowercase letters. Checking only the
    # LENGTH lets an ordinary account password through -- it is often 16 chars
    # too -- and the only feedback is a bare AUTHENTICATIONFAILED, which reads
    # like a typo rather than "you pasted the wrong kind of secret".
    import re as _re
    shape_ok = bool(_re.fullmatch(r"[a-z]{16}", pw))
    print(f"  password  {len(pw)} chars"
          + ("" if shape_ok else "   <- NOT an App Password shape"))
    if not shape_ok:
        classes = sorted({"digit" if c.isdigit() else "uppercase" if c.isupper()
                          else "lowercase" if c.islower() else f"symbol {c!r}"
                          for c in pw})
        print(f"\n  FAIL  This is not a Google App Password.")
        print(f"        An App Password is 16 LOWERCASE LETTERS and nothing else.")
        print(f"        What is in .env contains: {', '.join(classes)}.")
        print(f"        That looks like an ordinary account password, which Gmail")
        print(f"        IMAP will always reject -- and which should not sit in a file.\n")
        print(f"  1. 2-Step Verification must be ON first, or the App Passwords")
        print(f"     page will not exist: https://myaccount.google.com/security")
        print(f"  2. Then create one at: https://myaccount.google.com/apppasswords")
        print(f"  3. Replace GMAIL_APP_PASSWORD in .env with those 16 letters.")
        return 1
    print(f"  host      {gmail.IMAP_HOST}:{gmail.IMAP_PORT}")
    print(f"  folder    {gmail.IMAP_FOLDER}\n")

    try:
        conn = gmail.connect()
    except gmail.GmailAuthError as e:
        print(f"  FAIL  {e}")
        return 1
    print("  OK    login")

    try:
        typ, _ = conn.select(gmail._quote(gmail.IMAP_FOLDER), readonly=True)
        if typ != "OK":
            print(f"  FAIL  cannot open {gmail.IMAP_FOLDER!r}. If your Gmail uses "
                  f"localised folder names, set GMAIL_IMAP_FOLDER in .env.")
            return 1
        print(f"  OK    opened {gmail.IMAP_FOLDER}")
    finally:
        try:
            conn.close()
            conn.logout()
        except Exception:
            pass

    from jobpipe.sources.gmail_alerts import QUERY
    print(f"\n  alert query: {QUERY}")
    try:
        # Deliberately ABOVE GMAIL_ALERT_MAX. This preflight exists to tell you
        # how much mail is arriving; capping it at the ingest cap makes it report
        # its own limit as a measurement. That is precisely what "25 message(s)
        # matched" meant while 62 emails were arriving in the same window.
        cap = env_int("GMAIL_ALERT_MAX", 60)
        probe = max(cap * 4, 200)
        msgs = gmail.search(QUERY, max_results=probe)
    except Exception as e:
        print(f"  FAIL  search: {type(e).__name__}: {e}")
        return 1

    print(f"  OK    {len(msgs)} message(s) matched")
    if len(msgs) > cap:
        print(f"  WARN  ingest reads only the newest {cap} of these"
              f" -- {len(msgs) - cap} would be dropped in silence."
              f" Raise GMAIL_ALERT_MAX or cut a portal's alert count.")
    elif len(msgs) >= probe:
        print(f"  WARN  hit this preflight's own {probe} probe limit;"
              f" the real total is higher.")
    if not msgs:
        print("\n  Authenticated fine, but nothing matched. That is a QUERY problem,")
        print("  not a credential problem: either no alert mail has arrived in the")
        print("  window, or the senders differ. Set GMAIL_ALERT_QUERY in .env --")
        print('  a hand-maintained label beats a sender list, e.g.')
        print('      GMAIL_ALERT_QUERY=label:"Job Alerts" newer_than:7d')
        return 1

    senders: dict[str, int] = {}
    for m in msgs:
        frm = gmail.headers(m).get("from", "?")
        senders[frm[-60:]] = senders.get(frm[-60:], 0) + 1
    print("\n  by sender:")
    for frm, n in sorted(senders.items(), key=lambda kv: -kv[1]):
        print(f"    {n:3}  {frm}")

    body = gmail.body_text(msgs[0])
    print(f"\n  newest message body: {len(body)} chars of readable text")
    if len(body) < 200:
        print("    <- suspiciously short. 7.34: HTML-only mail returned raw made")
        print("       the model read a stylesheet and find no jobs, silently.")
    print("\nReady. `make ingest` will use IMAP.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
