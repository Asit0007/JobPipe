"""Telegram review queue. Free, and you already run a bot for QuantBot.

Sends the day's shortlist as individual messages with the apply link. Tapping
the link opens the real posting in your browser, where YOU apply.
"""
from __future__ import annotations

import json

import httpx

from . import db
from .config import env, profile
from .db import now

API = "https://api.telegram.org/bot{token}/sendMessage"


def _send(text: str) -> bool:
    token, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print(text)
        print("-" * 60)
        return False
    r = httpx.post(API.format(token=token), timeout=20, json={
        "chat_id": chat, "text": text,
        "parse_mode": "Markdown", "disable_web_page_preview": True,
    })
    return r.status_code == 200


def run(log=print) -> None:
    cap = profile()["thresholds"]["notify_daily_cap"]
    rows = db.fetch(status="prepared", limit=cap)
    if not rows:
        log("nothing prepared to notify")
        return

    _send(f"*{len(rows)} roles ready for review* - {now()[:10]}\nYou apply. I don't.")

    for job in rows:
        missing = json.loads(job["missing_skills"] or "[]")
        flags = json.loads(job["red_flags"] or "[]")
        msg = (
            f"*{job['score']}* | {job['title']}\n"
            f"{job['company']} - {job['location'] or 'n/a'}\n\n"
            f"_{job['score_reason']}_\n"
        )
        if missing:
            msg += f"\nGaps: {', '.join(missing[:4])}"
        if flags:
            msg += f"\nFlags: {', '.join(flags[:2])}"
        msg += f"\n\n[Open posting]({job['apply_url'] or job['url']})"
        if _send(msg):
            db.update(job["id"], status="queued", notified_at=now())

    log(f"queued {len(rows)} for review")
    db.log_run("notify", True, f"{len(rows)} sent")
