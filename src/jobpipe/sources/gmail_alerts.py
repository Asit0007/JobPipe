"""Parse LinkedIn / Naukri / Indeed job-alert emails out of your own inbox.

This is the ToS-clean route to the two boards you cannot touch directly. You
configure the saved searches on their site; they push results to you; we read
your mail. Zero account activity on either platform.

Link extraction is deterministic (regex on the anchor hrefs). Gemini is used
only to pull structured title/company pairs out of the surrounding text, which
varies wildly between senders and breaks constantly under pure regex.
"""
from __future__ import annotations

import re

from ..config import MODEL_SCORE, env
from ..llm import generate_json
from .base import make_job

QUERY = (
    'newer_than:3d ('
    'from:jobalerts-noreply@linkedin.com OR '
    'from:jobs-listings@linkedin.com OR '
    'from:info@naukri.com OR from:alerts@naukri.com OR '
    'from:alert@indeed.com OR from:noreply@indeed.com'
    ')'
)

LINK_RE = re.compile(r'https?://[^\s"\'<>)]+', re.I)
JOB_LINK_HINTS = ("linkedin.com/jobs/view", "naukri.com/job-listings", "indeed.com/rc/clk",
                  "indeed.com/viewjob", "linkedin.com/comm/jobs/view")

PROMPT = """Extract every distinct job posting from this job-alert email.

Return JSON: {{"jobs":[{{"title":"...","company":"...","location":"..."}}]}}

Rules:
- One entry per posting. Do not invent postings that are not present.
- If a field is absent, use an empty string. Do not guess.
- Ignore promotional content, footers, and "jobs you may be interested in" upsells.

EMAIL:
{body}
"""


def fetch(log=print) -> list[dict]:
    try:
        from ..gmail import body_text, headers, search
        msgs = search(QUERY, max_results=25)
    except Exception as e:
        log(f"  gmail_alert: {type(e).__name__} -- skipping ({e})")
        return []

    jobs: list[dict] = []
    for msg in msgs:
        text = body_text(msg)
        if not text:
            continue
        sender = headers(msg).get("from", "")
        board = ("linkedin" if "linkedin" in sender else
                 "naukri" if "naukri" in sender else
                 "indeed" if "indeed" in sender else "email")

        links = [u for u in LINK_RE.findall(text) if any(h in u for h in JOB_LINK_HINTS)]

        try:
            parsed = generate_json(
                PROMPT.format(body=text[:12000]), model=MODEL_SCORE, temperature=0.0
            )
        except Exception as e:
            log(f"  gmail_alert: parse failed ({type(e).__name__})")
            continue

        for i, j in enumerate(parsed.get("jobs", [])):
            if not j.get("title") or not j.get("company"):
                continue
            url = links[i] if i < len(links) else (links[0] if links else "")
            if not url:
                continue
            jobs.append(make_job(
                source=f"alert:{board}",
                source_id=None,
                company=j["company"],
                title=j["title"],
                location=j.get("location", ""),
                url=url,
                description="",   # JD fetched at score time or read on the site
            ))
    log(f"  gmail_alert: {len(jobs)} from {len(msgs)} emails")
    return jobs
