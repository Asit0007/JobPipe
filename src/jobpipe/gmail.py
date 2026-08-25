"""Gmail helper. Read-only scope.

This is how LinkedIn and Naukri enter the pipeline: you set up saved-search
alerts on those sites, they email you, and we parse YOUR OWN INBOX. No scraping,
no stored password, no session automation, nothing for either platform to detect.
"""
from __future__ import annotations

import base64
from pathlib import Path

from .config import env

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


def body_text(msg: dict) -> str:
    def walk(part) -> str:
        if part.get("mimeType", "").startswith("text/") and part.get("body", {}).get("data"):
            raw = base64.urlsafe_b64decode(part["body"]["data"])
            return raw.decode("utf-8", errors="replace")
        return "".join(walk(p) for p in part.get("parts", []) or [])
    return walk(msg.get("payload", {}))
