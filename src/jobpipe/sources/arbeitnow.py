"""Arbeitnow public job-board feed. No auth, no key.

Its descriptions arrive HTML-ESCAPED -- the payload literally contains
"&lt;div class=&quot;content-intro&quot;&gt;". Running strip_html over that
removes nothing, because there are no real tags to remove, and the scorer then
reads a wall of entities as if it were the job description. Unescape first,
then strip. Verified against the live feed.
"""
from __future__ import annotations

import html
from datetime import datetime, timezone

from .base import get_json, make_job, strip_html

FEED = "https://www.arbeitnow.com/api/job-board-api"


def _iso(epoch) -> str | None:
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def fetch(log=print) -> list[dict]:
    try:
        data = get_json(FEED)
    except Exception as e:
        log(f"  arbeitnow: {type(e).__name__} -- skipping source")
        return []

    jobs = []
    for j in data.get("data", []) or []:
        title, company = (j.get("title") or "").strip(), (j.get("company_name") or "").strip()
        if not (title and company):
            continue
        loc = (j.get("location") or "").strip()
        if j.get("remote") and "remote" not in loc.lower():
            loc = f"{loc} (Remote)".strip()
        jobs.append(make_job(
            source="arbeitnow",
            source_id=j.get("slug"),
            company=company,
            title=title,
            location=loc,
            url=j.get("url", ""),
            description=strip_html(html.unescape(j.get("description") or "")),
            posted_at=_iso(j.get("created_at")),
        ))
    log(f"  arbeitnow: {len(jobs)}")
    return jobs
