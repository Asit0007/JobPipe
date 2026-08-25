"""Reply tracking. Reads your inbox, classifies responses, flags follow-ups.

Matching is the hard part and the old version got it wrong three ways:

  1. A substring test of a canonicalised company name against from+subject.
     Short names -- ramp, linear, vanta -- matched unrelated mail constantly.
  2. Applications were keyed by company, so two applications to the same
     company collided and only the last one could ever be tracked.
  3. Every message rescanned every application, O(n*m).

Now: candidates are indexed by job id, a match needs at least two independent
signals, and same-company applications are separated by title tokens.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

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

MIN_SIGNALS = 2          # never classify on one weak coincidence

# Two different jobs, so two different lists. Company tokens must exclude
# industry words, or an employer called "Cloud Systems" matches every
# infrastructure posting in the inbox. Title tokens must NOT exclude them --
# "site reliability engineer" appearing in a subject line is exactly the
# evidence we are looking for, and stopwording it away is what made a real
# reply from careers@<company>.com score only one signal.
_COMPANY_STOP = {
    "the", "and", "for", "with", "india", "global", "group", "labs", "inc",
    "cloud", "systems", "technologies", "solutions", "services", "software",
    "digital", "data", "tech", "consulting", "networks",
}
_TITLE_STOP = {"the", "and", "for", "with", "your", "our", "job", "role",
               "application", "applying", "position", "opportunity"}
# ATS senders relay mail on the employer's behalf; their domain is a real signal
# that this is application mail, but it is never company-identifying on its own.
ATS_DOMAINS = ("greenhouse.io", "greenhouse-mail.io", "lever.co", "ashbyhq.com",
               "workable.com", "myworkday.com", "smartrecruiters.com", "icims.com")


def _tokens(text: str, stop: set[str] = frozenset()) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{4,}", (text or "").lower())
            if w not in stop}


def _domain(url: str) -> str:
    host = urlparse(url or "").netloc.lower().removeprefix("www.")
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _signals(job, sender: str, subject: str) -> tuple[int, list[str]]:
    """Independent reasons to believe this email is about this application."""
    sender, subject = sender.lower(), subject.lower()
    sender_domain = sender.split("@")[-1].strip("> ")
    company_tokens = _tokens(canon_company(job["company"]), _COMPANY_STOP)
    found = []

    # The employer's own mail server. The strongest signal there is.
    if any(tok in sender_domain for tok in company_tokens):
        found.append(f"sender-domain:{sender_domain}")

    # The apply link's own domain, when it is the employer's and not the ATS's.
    dom = _domain(job["apply_url"] or job["url"])
    if dom and not any(a in dom for a in ATS_DOMAINS) and dom in sender:
        found.append(f"apply-domain:{dom}")

    for tok in company_tokens:
        if re.search(rf"(?<![a-z-]){re.escape(tok)}(?![a-z])", subject):
            found.append(f"subject-company:{tok}")
            break

    title_hits = _tokens(job["title"], _TITLE_STOP) & _tokens(subject, _TITLE_STOP)
    if len(title_hits) >= 2:
        found.append(f"title:{'+'.join(sorted(title_hits)[:2])}")

    if any(d in sender_domain for d in ATS_DOMAINS):
        found.append("ats-sender")

    return len(found), found


def run(log=print) -> None:
    try:
        from .gmail import body_text, headers, search
        msgs = search("newer_than:7d -category:promotions", max_results=40)
    except Exception as e:
        log(f"gmail unavailable: {type(e).__name__} -- skipping tracking")
        return

    applied = db.fetch(status="applied", limit=500)
    if not applied:
        log("no applied jobs to track yet")
        return

    # Index once, by company token, instead of rescanning every application per
    # message. Same-company applications all live under the same key and are
    # separated afterwards by signal strength.
    by_token: dict[str, list] = defaultdict(list)
    for job in applied:
        for tok in _tokens(canon_company(job["company"]), _COMPANY_STOP):
            by_token[tok].append(job)

    matched = 0
    for msg in msgs:
        h = headers(msg)
        sender, subject = h.get("from", ""), h.get("subject", "")
        hay_tokens = _tokens(f"{sender} {subject}", _COMPANY_STOP)

        candidates = {j["id"]: j for tok in hay_tokens for j in by_token.get(tok, [])}
        if not candidates:
            continue

        ranked = []
        for job in candidates.values():
            n, why = _signals(job, sender, subject)
            if n >= MIN_SIGNALS:
                ranked.append((n, job, why))
        if not ranked:
            continue

        ranked.sort(key=lambda x: (-x[0], -(x[1]["applied_at"] or "") .__len__()))
        best_n, job, why = ranked[0]

        # Two applications matching equally well is genuinely ambiguous. Say so
        # rather than picking one and silently closing the wrong application.
        tied = [r for r in ranked if r[0] == best_n]
        if len(tied) > 1:
            log(f"  ambiguous: {len(tied)} applications match '{subject[:40]}' "
                f"-- {', '.join(f'#{t[1]['id']} {t[1]['title'][:25]}' for t in tied)}")
            continue

        try:
            res = generate_json(
                PROMPT.format(subject=subject, sender=sender,
                              body=body_text(msg)[:3000]),
                model=MODEL_SCORE, temperature=0.0,
            )
        except Exception:
            continue
        if res.get("kind") in (None, "noise"):
            continue

        db.update(job["id"], status="responded", response_at=now(),
                  response_kind=res["kind"])
        matched += 1
        log(f"  {res['kind']}: {job['company']} - {job['title'][:40]} [{', '.join(why)}]")

    # Follow-up nudges
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    stale = [r for r in db.fetch(status="applied", limit=200)
             if (r["applied_at"] or "") < cutoff and not r["response_at"]]
    for r in stale:
        db.update(r["id"], followup_due=now())
    log(f"{matched} responses classified | {len(stale)} due a follow-up nudge")
    db.log_run("track", True, f"{matched} responses")
