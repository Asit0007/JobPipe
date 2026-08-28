"""Himalayas public remote-jobs feed. No auth, no key.

No id field -- `guid` (the canonical posting URL) is the stable identifier.
`locationRestrictions` is a list and is frequently empty, which means
"anywhere" rather than "unknown", so an empty one becomes "Remote".
`limit` is accepted but the feed caps out around 20 rows regardless.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .base import get_json, make_job, strip_html

FEED = "https://himalayas.app/jobs/api?limit=100"


def _iso(epoch) -> str | None:
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _salary(j) -> str | None:
    lo, hi, cur = j.get("minSalary"), j.get("maxSalary"), j.get("currency") or ""
    if not (lo or hi):
        return None
    return f"{lo or '?'}-{hi or '?'} {cur} {j.get('salaryPeriod') or ''}".strip()


def fetch(log=print) -> list[dict]:
    try:
        data = get_json(FEED)
    except Exception as e:
        log(f"  himalayas: {type(e).__name__} -- skipping source")
        return []

    jobs = []
    for j in data.get("jobs", []) or []:
        title, company = (j.get("title") or "").strip(), (j.get("companyName") or "").strip()
        if not (title and company):
            continue
        restrictions = j.get("locationRestrictions") or []
        location = ", ".join(str(r) for r in restrictions) if restrictions else "Remote"
        jobs.append(make_job(
            source="himalayas",
            source_id=j.get("guid"),
            company=company,
            title=title,
            location=location,
            url=j.get("guid") or j.get("applicationLink") or "",
            apply_url=j.get("applicationLink"),
            description=strip_html(j.get("description") or j.get("excerpt") or ""),
            salary_raw=_salary(j),
            posted_at=_iso(j.get("pubDate")),
        ))
    log(f"  himalayas: {len(jobs)}")
    return jobs
