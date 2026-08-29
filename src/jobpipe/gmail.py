"""Gmail helper. Read-only scope.

This is how LinkedIn and Naukri enter the pipeline: you set up saved-search
alerts on those sites, they email you, and we parse YOUR OWN INBOX. No scraping,
no stored password, no session automation, nothing for either platform to detect.
"""
from __future__ import annotations

import base64
import re
from pathlib import Path

from .config import env
from .sources.base import strip_html

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


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
                    f"Missing {creds_path}. See README step 4 for the 2-minute setup."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def search(query: str, max_results: int = 50) -> list[dict]:
    svc = service()
    res = svc.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    out = []
    for m in res.get("messages", []):
        full = svc.users().messages().get(userId="me", id=m["id"], format="full").execute()
        out.append(full)
    return out


def headers(msg: dict) -> dict:
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


def body_text(msg: dict) -> str:
    """The message as readable text.

    Alert mail is HTML-only in practice -- measured 2026-08-29, all 25 alert
    emails in the inbox carried a single text/html part and no text/plain at
    all. Returning that markup raw meant `gmail_alerts` truncated it at 12,000
    chars and handed the model a Glassdoor email's <head>: preload links and
    CSS, not one job. It parsed 0 jobs from 25 emails and reported no error,
    because finding nothing in a stylesheet is not a failure.
    """
    plain, html = [], []

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
