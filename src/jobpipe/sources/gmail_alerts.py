"""Parse LinkedIn / Naukri / Indeed job-alert emails out of your own inbox.

This is the ToS-clean route to the two boards you cannot touch directly. You
configure the saved searches on their site; they push results to you; we read
your mail. Zero account activity on either platform.

Link extraction is deterministic (regex on the anchor hrefs). Gemini is used
only to pull structured title/company pairs out of the surrounding text, which
varies wildly between senders and breaks constantly under pure regex.

Links are matched to postings BY POSITION IN THE TEXT, not by array index.
Pairing links[i] to jobs[i] assumes the regex and the model enumerate in the
same order; one interleaved promo link, or one posting the model merges or
drops, and every job after it silently gets someone else's URL -- which is
worse than having no URL at all.
"""
from __future__ import annotations

import re

from ..config import MODEL_SCORE
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

# How far from a title a link may sit and still be considered its link. Anchor
# markup puts the href just before the visible text, so the window is asymmetric
# in practice but a symmetric bound is easier to reason about.
MAX_LINK_DISTANCE = 2000

PROMPT = """Extract every distinct job posting from this job-alert email.

Return JSON: {{"jobs":[{{"title":"...","company":"...","location":"...","snippet":"..."}}]}}

Rules:
- One entry per posting. Do not invent postings that are not present.
- Copy the title EXACTLY as it appears in the email, character for character.
  It is used to locate the posting's link, so a paraphrase breaks the match.
- If a field is absent, use an empty string. Do not guess.
- Ignore promotional content, footers, and "jobs you may be interested in" upsells.
- "snippet" is whatever description, blurb, requirement list or skill tags the
  email shows for THAT posting. Copy it VERBATIM from the email. Do not
  summarise it, do not tidy it up, and do not write one from the job title --
  this text is scored as if it were the job description, so an invented
  snippet produces an invented score. If the email shows no description for a
  posting, return "" and let the posting page supply it later.

EMAIL:
{body}
"""


def _job_links(text: str) -> list[tuple[int, str]]:
    """Every posting link, with where it sits in the text. Order preserved."""
    return [(m.start(), m.group(0)) for m in LINK_RE.finditer(text)
            if any(h in m.group(0) for h in JOB_LINK_HINTS)]


def _match_link(title: str, text: str, links: list[tuple[int, str]],
                claimed: set[str]) -> str:
    """The unclaimed posting link nearest to where this title appears."""
    if not links or not title:
        return ""
    pos = text.lower().find(title.lower().strip())
    if pos < 0:
        return ""                       # model paraphrased; refuse to guess
    candidates = [(abs(lp - pos), url) for lp, url in links if url not in claimed]
    if not candidates:
        return ""
    distance, url = min(candidates)
    return url if distance <= MAX_LINK_DISTANCE else ""


def fetch(log=print) -> list[dict]:
    try:
        from ..gmail import body_text, headers, search
        msgs = search(QUERY, max_results=25)
    except Exception as e:
        log(f"  gmail_alert: {type(e).__name__} -- skipping ({e})")
        return []

    jobs: list[dict] = []
    unmatched = 0
    for msg in msgs:
        text = body_text(msg)
        if not text:
            continue
        sender = headers(msg).get("from", "")
        board = ("linkedin" if "linkedin" in sender else
                 "naukri" if "naukri" in sender else
                 "indeed" if "indeed" in sender else "email")

        links = _job_links(text)

        try:
            parsed = generate_json(
                PROMPT.format(body=text[:12000]), model=MODEL_SCORE, temperature=0.0
            )
        except Exception as e:
            log(f"  gmail_alert: parse failed ({type(e).__name__})")
            continue

        claimed: set[str] = set()
        for j in parsed.get("jobs", []):
            if not j.get("title") or not j.get("company"):
                continue
            url = _match_link(j["title"], text, links, claimed)
            if not url:
                # No confident link. Dropping it beats attaching the wrong one:
                # a wrong URL sends you to apply for somebody else's job.
                unmatched += 1
                continue
            claimed.add(url)
            jobs.append(make_job(
                source=f"alert:{board}",
                source_id=None,
                company=j["company"],
                title=j["title"],
                location=j.get("location", ""),
                url=url,
                # Whatever blurb the email carried. Usually short and often
                # empty -- LinkedIn and Indeed give a line or two, Naukri gives
                # skill tags. It is provisional: jdfetch still re-fetches
                # anything under MIN_USEFUL_CHARS and overwrites this with the
                # real posting page, and upsert_job backfills over an empty
                # one. Storing it costs nothing and, for the boards jdfetch
                # cannot reach (LinkedIn, Indeed, Glassdoor), it is the only
                # description the row will ever have.
                description=(j.get("snippet") or "").strip(),
            ))

    log(f"  gmail_alert: {len(jobs)} from {len(msgs)} emails"
        + (f" ({unmatched} dropped, no confident link)" if unmatched else ""))
    return jobs
