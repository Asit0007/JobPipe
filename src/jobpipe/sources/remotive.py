"""Remotive public remote-jobs feed. No auth, no key.

Global feed, not company-scoped, so there is no slug to maintain. It returns
its own small working set -- measured 2026-08-28, 18 rows with or without a
`search`/`category` parameter, so no query filtering is applied here. The
keyword prefilter is free and already discards ~80%; asking this API to
pre-filter would only add a brittle keyword list for no gain.
"""
from __future__ import annotations

from .base import get_json, make_job, strip_html

FEED = "https://remotive.com/api/remote-jobs"


def fetch(log=print) -> list[dict]:
    try:
        data = get_json(FEED)
    except Exception as e:
        log(f"  remotive: {type(e).__name__} -- skipping source")
        return []

    jobs = []
    for j in data.get("jobs", []) or []:
        title, company = (j.get("title") or "").strip(), (j.get("company_name") or "").strip()
        if not (title and company):
            continue
        jobs.append(make_job(
            source="remotive",
            source_id=j.get("id"),
            company=company,
            title=title,
            # Remotive states eligibility ("LATAM, Europe, USA") rather than a
            # place. It is still the only location signal the row carries, and
            # the scorer reads it as such.
            location=j.get("candidate_required_location") or "Remote",
            url=j.get("url", ""),
            description=strip_html(j.get("description", "")),
            salary_raw=(j.get("salary") or "").strip() or None,
            posted_at=j.get("publication_date"),
        ))
    log(f"  remotive: {len(jobs)}")
    return jobs
