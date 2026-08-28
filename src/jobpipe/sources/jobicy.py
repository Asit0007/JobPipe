"""Jobicy public remote-jobs feed. No auth, no key.

Field names are camelCase and none of them match the obvious guess: the title
is `jobTitle`, the company is `companyName`, the body is `jobDescription`.
`count` maxes out around 100. An `industry` filter exists but returns a payload
with no `jobs` key at all for the values worth using, so it is left off.
"""
from __future__ import annotations

from .base import get_json, make_job, strip_html

FEED = "https://jobicy.com/api/v2/remote-jobs?count=100"


def fetch(log=print) -> list[dict]:
    try:
        data = get_json(FEED)
    except Exception as e:
        log(f"  jobicy: {type(e).__name__} -- skipping source")
        return []

    jobs = []
    for j in data.get("jobs", []) or []:
        title, company = (j.get("jobTitle") or "").strip(), (j.get("companyName") or "").strip()
        if not (title and company):
            continue
        # jobDescription is the full body; jobExcerpt is a summary. Prefer the
        # body and fall back, because the prefilter counts keywords in it.
        body = j.get("jobDescription") or j.get("jobExcerpt") or ""
        jobs.append(make_job(
            source="jobicy",
            source_id=j.get("id"),
            company=company,
            title=title,
            location=(j.get("jobGeo") or "").strip() or "Remote",
            url=j.get("url", ""),
            description=strip_html(body),
            posted_at=j.get("pubDate"),
        ))
    log(f"  jobicy: {len(jobs)}")
    return jobs
