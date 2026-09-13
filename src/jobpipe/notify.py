"""Telegram review queue. Free, and you already run a bot for QuantBot.

Sends the day's shortlist as individual messages with the apply link. Tapping
the link opens the real posting in your browser, where YOU apply.
"""
from __future__ import annotations

import json

import httpx

from . import cooldown, db
from .config import env, profile
from .db import now

API = "https://api.telegram.org/bot{token}/sendMessage"


# Telegram's legacy Markdown treats these as formatting. A job title like
# "Linux Administration_94357" therefore opens an italic run that never closes,
# and the API answers 400 "can't parse entities" -- measured 2026-09-13, which
# is how one prepared document sat un-notified while the run said it had queued
# it. Escape every INTERPOLATED value; the literal * and _ in the template are
# the formatting we actually want.
_MD_SPECIALS = ("\\", "_", "*", "`", "[")


def _md(value) -> str:
    out = str(value or "")
    for ch in _MD_SPECIALS:
        out = out.replace(ch, "\\" + ch)
    return out


def _send(text: str) -> bool:
    token, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print(text)
        print("-" * 60)
        return False
    payload = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
    r = httpx.post(API.format(token=token), timeout=20,
                   json={**payload, "parse_mode": "Markdown"})
    if r.status_code == 200:
        return True
    # Fail OPEN, never silent. Escaping should make this unreachable, but a
    # message that cannot be formatted must still arrive -- losing a job
    # notification to a stray punctuation mark is the worst trade available
    # here. Retry once as plain text, which has no parse step to fail.
    if r.status_code == 400:
        plain = httpx.post(API.format(token=token), timeout=20, json=payload)
        if plain.status_code == 200:
            return True
        r = plain
    try:
        why = r.json().get("description", r.text[:120])
    except Exception:
        why = r.text[:120]
    print(f"  ! telegram refused ({r.status_code}): {why}")
    return False


def run(log=print) -> None:
    cap = profile()["thresholds"]["notify_daily_cap"]
    rows = db.fetch(status="prepared", limit=cap)
    if not rows:
        log("nothing prepared to notify")
        return

    _send(f"*{len(rows)} roles ready for review* - {now()[:10]}\nYou apply. I don't.")

    # Built once for the whole batch: check() would otherwise re-read the
    # applied and in-flight tables for every message.
    applied_idx, flight_idx = cooldown.applied_index(), cooldown.in_flight_index()

    sent = 0
    for job in rows:
        missing = json.loads(job["missing_skills"] or "[]")
        flags = json.loads(job["red_flags"] or "[]")
        msg = (
            f"*{job['score']}* | {_md(job['title'])}\n"
            f"{_md(job['company'])} - {_md(job['location'] or 'n/a')}\n\n"
            f"_{_md(job['score_reason'])}_\n"
        )
        if missing:
            msg += f"\nGaps: {_md(', '.join(missing[:4]))}"
        if flags:
            msg += f"\nFlags: {_md(', '.join(flags[:2]))}"
        # Telegram is where a role is seen FIRST, so a warning missing here is
        # a warning that arrives too late to change anything.
        warn = cooldown.line(cooldown.check(
            job["company"], job_id=job["id"],
            applied=applied_idx, in_flight=flight_idx))
        if warn:
            msg += f"\n\n*{_md(warn)}*"
        msg += f"\n\n[Open posting]({job['apply_url'] or job['url']})"
        if _send(msg):
            db.update(job["id"], status="queued", notified_at=now())
            sent += 1

    # Report what was SENT, not what was attempted. These differed on
    # 2026-09-13 -- 15 attempted, 14 delivered, and the run printed "queued 15"
    # while one document sat in `prepared` with no notified_at. A count that
    # cannot be wrong about its own failure is the whole point of counting.
    failed = len(rows) - sent
    log(f"queued {sent} for review" + (f"  ({failed} refused by telegram)" if failed else ""))
    db.log_run("notify", failed == 0, f"{sent} sent, {failed} failed")
