"""Reply tracking. Reads your inbox, classifies responses, flags follow-ups."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import db
from .config import MODEL_SCORE
from .db import now
from .llm import generate_json
from .normalize import canon_company

PROMPT = """Classify this email from a company you applied to.

Return JSON: {{"kind":"rejection"|"recruiter"|"interview"|"offer"|"noise","company":"<company name or empty>"}}

- "recruiter" = a human asking to talk or requesting details
- "interview" = a scheduled round or an assessment link
- "noise"     = newsletters, job alerts, automated acknowledgements

EMAIL
Subject: {subject}
From: {sender}
{body}
"""


def run(log=print) -> None:
    try:
        from .gmail import body_text, headers, search
        msgs = search("newer_than:7d -category:promotions", max_results=40)
    except Exception as e:
        log(f"gmail unavailable: {type(e).__name__} -- skipping tracking")
        return

    applied = {canon_company(r["company"]): r for r in db.fetch(status="applied", limit=500)}
    if not applied:
        log("no applied jobs to track yet")

    matched = 0
    for msg in msgs:
        h = headers(msg)
        sender, subject = h.get("from", ""), h.get("subject", "")
        if not any(canon_company(c) and canon_company(c) in f"{sender} {subject}".lower()
                   for c in [r["company"] for r in applied.values()]):
            continue
        try:
            res = generate_json(
                PROMPT.format(subject=subject, sender=sender,
                              body=body_text(msg)[:3000]),
                model=MODEL_SCORE, temperature=0.0,
            )
        except Exception:
            continue
        if res.get("kind") == "noise":
            continue
        row = applied.get(canon_company(res.get("company", "")))
        if not row:
            continue
        db.update(row["id"], status="responded", response_at=now(),
                  response_kind=res["kind"])
        matched += 1
        log(f"  {res['kind']}: {row['company']}")

    # Follow-up nudges
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    stale = [r for r in db.fetch(status="applied", limit=200)
             if (r["applied_at"] or "") < cutoff and not r["response_at"]]
    for r in stale:
        db.update(r["id"], followup_due=now())
    log(f"{matched} responses classified | {len(stale)} due a follow-up nudge")
    db.log_run("track", True, f"{matched} responses")
