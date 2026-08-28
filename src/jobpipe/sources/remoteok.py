"""RemoteOK public feed. No auth, no key.

One trap: the response is a bare list whose FIRST element is a legal/metadata
object ({"last_updated": ..., "legal": ...}), not a job. Indexing from 0 gives
you a row with no title and no company. Rows are filtered on having both rather
than by position, so a change to where that object sits cannot break ingest.
"""
from __future__ import annotations

from .base import get_json, make_job, strip_html

FEED = "https://remoteok.com/api"


def _salary(j) -> str | None:
    lo, hi = j.get("salary_min") or 0, j.get("salary_max") or 0
    return f"{lo}-{hi} USD" if lo or hi else None


def fetch(log=print) -> list[dict]:
    try:
        data = get_json(FEED)
    except Exception as e:
        log(f"  remoteok: {type(e).__name__} -- skipping source")
        return []

    jobs = []
    for j in data or []:
        if not isinstance(j, dict):
            continue
        # "position", not "title" -- and the legal object has neither.
        title, company = (j.get("position") or "").strip(), (j.get("company") or "").strip()
        if not (title and company):
            continue
        jobs.append(make_job(
            source="remoteok",
            source_id=j.get("id") or j.get("slug"),
            company=company,
            title=title,
            location=(j.get("location") or "").strip() or "Remote",
            url=j.get("url", ""),
            apply_url=j.get("apply_url"),
            description=strip_html(j.get("description", "")),
            salary_raw=_salary(j),
            posted_at=j.get("date"),
        ))
    log(f"  remoteok: {len(jobs)}")
    return jobs
