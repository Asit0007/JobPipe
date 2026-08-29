"""One-shot Gmail OAuth, run deliberately instead of by accident.

`gmail.service()` will happily start a browser consent flow the first time it is
called -- which, left alone, happens in the middle of `make ingest`. That is the
worst possible moment to find out the credentials file is the wrong kind, or
that the token cannot be written. This does the same thing on purpose, verifies
the result against the live API, and says which mailbox it actually authorised.

    make gmail-auth PY=./.venv/bin/python

Read-only scope. Nothing here can send, delete or modify mail.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobpipe.config import env          # noqa: E402
from jobpipe.gmail import SCOPES        # noqa: E402

OK, WARN, BAD = "  ok  ", " warn ", " FAIL "


def line(tag: str, msg: str) -> None:
    print(f"[{tag}] {msg}")


def main() -> int:
    creds_path = Path(env("GMAIL_CREDENTIALS_JSON", "./data/gmail_credentials.json"))
    token_path = Path(env("GMAIL_TOKEN_JSON", "./data/gmail_token.json"))

    print(f"credentials: {creds_path}")
    print(f"token:       {token_path}")
    print(f"scope:       {SCOPES[0]}  (read-only)\n")

    if not creds_path.exists():
        line(BAD, f"{creds_path} does not exist.")
        print("""
  Google Cloud Console -> new project -> enable the Gmail API
  Credentials -> Create credentials -> OAuth client ID -> Desktop app
  Download the JSON and save it at the path above.

  It must be a DESKTOP app client. A "Web application" client hands back a
  redirect_uri mismatch against the loopback server this flow starts.""")
        return 1

    # A web-app client is the common wrong download and the error it eventually
    # produces names redirect URIs, not the client type. Catch it up front.
    try:
        blob = json.loads(creds_path.read_text())
        kind = next(iter(blob))
        if kind != "installed":
            line(BAD, f"{creds_path} is a '{kind}' client, not a Desktop app.")
            print("  Re-create the OAuth client as an 'installed'/Desktop app.")
            return 1
        line(OK, "credentials file is a Desktop-app client")
    except (json.JSONDecodeError, StopIteration):
        line(BAD, f"{creds_path} is not valid JSON.")
        return 1

    if token_path.exists():
        line(WARN, f"{token_path} already exists -- reusing it if still valid.")

    try:
        from jobpipe.gmail import service
        svc = service()
        me = svc.users().getProfile(userId="me").execute()
    except Exception as e:
        line(BAD, f"{type(e).__name__}: {e}")
        return 1

    line(OK, f"authorised as {me.get('emailAddress')} "
             f"({me.get('messagesTotal')} messages)")
    line(OK, f"token written to {token_path}")

    if token_path.exists():
        mode = token_path.stat().st_mode & 0o777
        if mode & 0o077:
            token_path.chmod(0o600)
            line(OK, "tightened token permissions to 0600")

    print("\nNext: set up saved-search alerts (Naukri first -- its postings can be")
    print("fetched in full, unlike LinkedIn/Indeed/Glassdoor), then `make ingest`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
