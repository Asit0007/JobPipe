"""Gmail, read-only. IMAP by default, OAuth as a deprecated fallback.

This is how LinkedIn and Naukri enter the pipeline: you set up saved-search
alerts on those sites, they email you, and we parse YOUR OWN INBOX. No scraping,
no stored board password, no session automation, nothing for either platform to
detect.

WHY IMAP RATHER THAN THE API
----------------------------
`gmail.readonly` is a *restricted* scope. An External OAuth app left in Testing
has its refresh tokens **expired after 7 days**, so a daily cron silently stops
pulling alerts every week. Neither escape works for this project:

  * Publishing is greyed out. Production for an External app with a restricted
    scope needs a verified domain, a privacy policy, and an annual third-party
    CASA security assessment.
  * Internal needs a Cloud Organization, and an Internal app can only be
    consented by accounts inside it -- while the mailbox holding the alerts is a
    personal @gmail.com.

An App Password has no expiry, needs no org, and costs nothing. Both callers
only ever READ, so IMAP covers the entire use case.

WHY THE QUERIES DID NOT HAVE TO CHANGE
--------------------------------------
Gmail's IMAP server implements `X-GM-RAW`, a SEARCH extension that takes a raw
**Gmail** search string. So `newer_than:3d`, `label:"Job Notifications"` and
`-category:promotions` all keep working exactly as written, and any
GMAIL_ALERT_QUERY already in a .env keeps working too. Translating Gmail syntax
into RFC 3501 SEARCH by hand would have been the obvious approach and would have
quietly changed what gets matched.

SETUP
-----
1. Turn on 2-Step Verification: myaccount.google.com/security
2. Create an App Password:      myaccount.google.com/apppasswords
3. Put it in .env:
       GMAIL_ADDRESS=you@gmail.com
       GMAIL_APP_PASSWORD=abcd efgh ijkl mnop     # spaces are fine
4. Verify:  make gmail-imap-check PY=./.venv/bin/python
"""
from __future__ import annotations

import email
import imaplib
import re
import time
from contextlib import contextmanager
from email.header import decode_header, make_header
from email.message import Message
from pathlib import Path

from .config import env
from .sources.base import strip_html

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

IMAP_HOST = env("GMAIL_IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(env("GMAIL_IMAP_PORT", "993") or 993)
# "[Gmail]/All Mail" rather than INBOX: X-GM-RAW then searches the same corpus
# the Gmail web UI does, so a query that works in the search box works here.
# An alert filed under a label and skipped from the inbox is still found.
IMAP_FOLDER = env("GMAIL_IMAP_FOLDER", "[Gmail]/All Mail")
# Seconds any one socket operation may wait. imaplib's default is to wait
# forever, and on 2026-10-06 it did: the connection to imap.gmail.com stayed
# ESTABLISHED with nothing arriving, `daily` sat in ingest for five days, and
# launchd will not start a job that is still running -- so every later day's run
# was skipped without a log line. A timeout fails the source; ingest moves on.
IMAP_TIMEOUT = float(env("GMAIL_IMAP_TIMEOUT", "60") or 60)
# Seconds to wait before trying a refused login once more. On 2026-10-27 Gmail
# answered AUTHENTICATIONFAILED to the second of four logins made within
# seconds, then took the same App Password a minute later -- and the error text
# told the owner to make a new one. One retry tells a refusal like that from a
# password that is really wrong, before anyone is told to replace it.
LOGIN_RETRY_SECONDS = float(env("GMAIL_LOGIN_RETRY_SECONDS", "60") or 60)


class GmailAuthError(RuntimeError):
    """Raised with the fix in the message, not just the failure."""


def _app_password() -> tuple[str, str] | None:
    """(address, password), or None when IMAP is not configured.

    An App Password is displayed in groups of four; people paste it with the
    spaces in. Google accepts it either way, so strip them rather than making
    the user wonder why a correct password is rejected.
    """
    address = (env("GMAIL_ADDRESS") or "").strip()
    password = (env("GMAIL_APP_PASSWORD") or "").replace(" ", "").strip()
    return (address, password) if address and password else None


def connect() -> imaplib.IMAP4_SSL:
    creds = _app_password()
    if not creds:
        raise GmailAuthError(
            "GMAIL_ADDRESS / GMAIL_APP_PASSWORD are not set. Create an App "
            "Password at myaccount.google.com/apppasswords (2-Step Verification "
            "must be on first), then put both in .env."
        )
    address, password = creds
    for attempt in (1, 2):
        conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT)
        try:
            conn.login(address, password)
            return conn
        except imaplib.IMAP4.error as e:
            detail = str(e)
            try:
                conn.shutdown()
            except Exception:
                pass
            if "AUTHENTICATIONFAILED" not in detail.upper():
                raise GmailAuthError(f"Gmail IMAP login failed for {address}: {detail}") from e
            if attempt == 1:
                time.sleep(LOGIN_RETRY_SECONDS)
                continue
            raise GmailAuthError(
                f"Gmail refused the App Password for {address} twice, "
                f"{LOGIN_RETRY_SECONDS:.0f} s apart ({detail}). If it worked before, "
                "Google has probably revoked it (changing the account password revokes "
                "every App Password): make a new one at myaccount.google.com/apppasswords "
                "and put it in .env as GMAIL_APP_PASSWORD. On a first setup it has to be "
                "a 16-character App Password, not the account password, with 2-Step "
                "Verification on.") from e


def _quote(query: str) -> str:
    """IMAP quoted-string: backslash and double-quote are the only escapes."""
    return '"' + query.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _close(conn: imaplib.IMAP4_SSL) -> None:
    try:
        conn.close()
    except Exception:
        pass
    try:
        conn.logout()
    except Exception:
        pass


def _open() -> imaplib.IMAP4_SSL:
    """A logged-in, read-only session on IMAP_FOLDER."""
    conn = connect()
    ok, _ = conn.select(_quote(IMAP_FOLDER), readonly=True)
    if ok != "OK":
        _close(conn)
        raise GmailAuthError(
            f"Could not open the mailbox {IMAP_FOLDER!r}. Set GMAIL_IMAP_FOLDER "
            f"if your account uses localised Gmail folder names."
        )
    return conn


def _uids(conn: imaplib.IMAP4_SSL, query: str) -> list[bytes]:
    """Every UID matching a Gmail search. UIDs ascend with arrival, so the
    newest are last."""
    # X-GM-RAW takes Gmail search syntax verbatim -- see the module docstring.
    typ, data = conn.uid("SEARCH", "X-GM-RAW", _quote(query))
    if typ != "OK":
        raise GmailAuthError(f"Gmail IMAP search failed: {data!r}")
    return (data[0] or b"").split()


def _fetch_one(conn: imaplib.IMAP4_SSL, uid: bytes) -> Message | None:
    typ, payload = conn.uid("FETCH", uid, "(BODY.PEEK[])")
    if typ != "OK" or not payload or not isinstance(payload[0], tuple):
        return None
    return email.message_from_bytes(payload[0][1])


def _search_imap(query: str, max_results: int) -> list[Message]:
    conn = _open()
    try:
        # Both callers want recent mail, and the Gmail API returned newest-first.
        uids = _uids(conn, query)[-max_results:][::-1]
        return [m for m in (_fetch_one(conn, uid) for uid in uids) if m is not None]
    finally:
        _close(conn)


# Enough to decide whether a message is worth downloading. Message-ID is what
# track.py remembers a message by, so a re-read window never reports it twice.
HEADER_FIELDS = "FROM SUBJECT DATE MESSAGE-ID"
HEADER_BATCH = 200
_UID_RE = re.compile(rb"\bUID (\d+)")


def _headers_imap(conn: imaplib.IMAP4_SSL, query: str, max_results: int) -> list[dict]:
    uids = _uids(conn, query)[-max_results:]
    out: list[dict] = []
    for i in range(0, len(uids), HEADER_BATCH):
        chunk = b",".join(uids[i:i + HEADER_BATCH])
        typ, payload = conn.uid("FETCH", chunk,
                                f"(BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])")
        if typ != "OK":
            raise GmailAuthError(f"Gmail IMAP header fetch failed: {payload!r}")
        # One (b"<seq> (UID <uid> BODY[...] {n}", b"<headers>") tuple per
        # message, each followed by a bare b")".
        for part in payload or []:
            if not isinstance(part, tuple):
                continue
            m = _UID_RE.search(part[0])
            if m:
                out.append({"id": m.group(1).decode(),
                            "headers": headers(email.message_from_bytes(part[1]))})
    # FETCH answers in mailbox order, not in the order asked.
    out.sort(key=lambda r: int(r["id"]), reverse=True)
    return out


class _ImapSession:
    def __init__(self):
        self.conn = _open()

    def _again(self, read):
        """read(conn), and once more on a fresh login if Gmail drops the
        connection part-way. On 2026-10-27 a 60-day header read lost its socket
        (EOF) in the middle of a FETCH, and the same read worked 20 s later."""
        try:
            return read(self.conn)
        except (imaplib.IMAP4.abort, OSError):
            _close(self.conn)
            self.conn = _open()
            return read(self.conn)

    def search_ids(self, query: str, max_results: int = 500) -> set[str]:
        return self._again(lambda c: {u.decode() for u in _uids(c, query)})

    def search_headers(self, query: str, max_results: int = 500) -> list[dict]:
        return self._again(lambda c: _headers_imap(c, query, max_results))

    def fetch(self, ids) -> dict[str, Message]:
        def read(conn):
            out: dict[str, Message] = {}
            for i in ids:
                msg = _fetch_one(conn, i.encode())
                if msg is not None:
                    out[i] = msg
            return out
        return self._again(read)

    def close(self) -> None:
        _close(self.conn)


# ---------------------------------------------------------------------------
# Deprecated OAuth path. Kept so an existing token keeps working through the
# switch; it dies every 7 days and is not a base to build a cron on.
# ---------------------------------------------------------------------------
def service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    token_path = Path(env("GMAIL_TOKEN_JSON", "./data/gmail_token.json"))
    creds_path = Path(env("GMAIL_CREDENTIALS_JSON", "./data/gmail_credentials.json"))

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not creds_path.exists():
                raise FileNotFoundError(
                    f"Missing {creds_path}. Prefer the IMAP path: set GMAIL_ADDRESS "
                    f"and GMAIL_APP_PASSWORD in .env (see jobpipe.gmail docstring)."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _list_api(query: str, max_results: int) -> list[dict]:
    svc = service()
    res = svc.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    return res.get("messages", [])


def _search_api(query: str, max_results: int) -> list[dict]:
    svc = service()
    return [svc.users().messages().get(userId="me", id=m["id"], format="full").execute()
            for m in _list_api(query, max_results)]


class _ApiSession:
    """The OAuth path has no cheap header read, so its header rows carry the
    whole message under "msg" and fetch() has nothing left to do."""

    def search_ids(self, query: str, max_results: int = 500) -> set[str]:
        return {m["id"] for m in _list_api(query, max_results)}

    def search_headers(self, query: str, max_results: int = 500) -> list[dict]:
        return [{"id": m["id"], "headers": headers(m), "msg": m}
                for m in _search_api(query, max_results)]

    def fetch(self, ids) -> dict:
        return {}

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# public interface -- unchanged for callers
# ---------------------------------------------------------------------------
def backend() -> str:
    return "imap" if _app_password() else "oauth"


def configured() -> bool:
    """Whether either backend has credentials to try. Lets a caller tell
    "Gmail is not set up" (skip quietly) from "Gmail is set up and failed"
    (say so). A credentials file without a token does not count: the OAuth
    flow would open a browser and wait, and nobody is there at 05:00."""
    if _app_password():
        return True
    return Path(env("GMAIL_TOKEN_JSON", "./data/gmail_token.json")).exists()


def search(query: str, max_results: int = 50):
    """Messages matching a **Gmail** search string, newest first."""
    return (_search_imap(query, max_results) if _app_password()
            else _search_api(query, max_results))


@contextmanager
def session():
    """One login for all of a run's reads: search_ids(), search_headers() and
    fetch(). One login per read made four in a few seconds, and on 2026-10-27
    Gmail refused the second with AUTHENTICATIONFAILED, then accepted the same
    App Password a minute later.

    search_ids(query) -> ids of the matching messages, nothing downloaded, so
        a query can ask what only the server knows (which tab a message is in).
    search_headers(query, max_results) -> [{"id", "headers"}], newest first,
        without bodies. Downloading whole messages to find the few worth
        reading is what kept track.py's window small: it read the newest 40 of
        a week that held 362 (measured 2026-10-27), about 18 hours of mail.
    fetch(ids) -> {id: whole message}, for the ids worth reading.
    """
    s = _ImapSession() if _app_password() else _ApiSession()
    try:
        yield s
    finally:
        s.close()


def _decode(value: str) -> str:
    """RFC 2047 header -> text.

    The API returned headers already decoded; IMAP does not. Without this a
    subject like "=?UTF-8?B?...?=" reaches track.py as gibberish and its
    company-token matching silently stops finding anything.
    """
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def headers(msg) -> dict:
    if isinstance(msg, Message):
        return {k.lower(): _decode(v) for k, v in msg.items()}
    return {h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])}


_ANCHOR_RE = re.compile(r'(?is)<a\b[^>]*\bhref=["\']([^"\']+)["\'][^>]*>(.*?)</a>')


def _html_to_text(html: str) -> str:
    """Readable text, with every link left sitting next to its own anchor text.

    Both callers depend on that adjacency. `gmail_alerts._match_link` pairs a job
    title to a URL by POSITION (7.3 -- pairing by index put jobs on each other's
    links), and a bare `strip_html` deletes `<a href=...>` along with every other
    tag, so the URL would be gone before the matcher ever ran.
    """
    html = re.sub(r"(?is)<(script|style|head|title)[^>]*>.*?</\1>", " ", html)
    # Bare URL, NOT wrapped in <>: strip_html deletes anything matching
    # <[^>]+> a moment later, so an angle-bracketed marker removes itself.
    html = _ANCHOR_RE.sub(lambda m: f"{m.group(2)} {m.group(1)} ", html)
    text = strip_html(html)
    # Marketing mail pads the preheader with hundreds of zero-width characters
    # so the inbox preview stays short. They survive strip_html, and on a real
    # Glassdoor alert they filled the first ~900 characters on their own.
    text = re.sub(r"[​-‏⁠﻿­]+", "", text)
    # Table layouts leave long runs of whitespace-only lines behind.
    text = re.sub(r"\n[ \t]*(?:\n[ \t]*)+", "\n\n", text)
    return text.strip()


def _part_text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


def body_text(msg) -> str:
    """The message as readable text.

    Alert mail is HTML-only in practice -- measured 2026-08-29, all 25 alert
    emails in the inbox carried a single text/html part and no text/plain at
    all. Returning that markup raw meant `gmail_alerts` truncated it at 12,000
    chars and handed the model a Glassdoor email's <head>: preload links and
    CSS, not one job. It parsed 0 jobs from 25 emails and reported no error,
    because finding nothing in a stylesheet is not a failure.
    """
    plain: list[str] = []
    html: list[str] = []

    if isinstance(msg, Message):
        for part in msg.walk():
            if part.get_content_maintype() != "text":
                continue
            # An attached .html or .txt file is not the message body.
            if (part.get_content_disposition() or "") == "attachment":
                continue
            (html if part.get_content_subtype() == "html" else plain).append(_part_text(part))
    else:
        import base64

        def walk(part) -> None:
            mime = part.get("mimeType", "")
            data = part.get("body", {}).get("data")
            if data and mime.startswith("text/"):
                raw = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                (html if mime == "text/html" else plain).append(raw)
            for p in part.get("parts") or []:
                walk(p)

        walk(msg.get("payload", {}))

    joined_plain = "".join(plain).strip()
    if joined_plain:
        return joined_plain
    return _html_to_text("".join(html))
