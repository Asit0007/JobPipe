"""Ashby public job board API."""
from __future__ import annotations

from ..config import companies
from .base import get_json, make_job, strip_html

BOARD = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"


def fetch(log=print) -> list[dict]:
    jobs: list[dict] = []
    for slug in companies().get("ashby", []) or []:
        try:
            data = get_json(BOARD.format(slug=slug))
        except Exception as e:
            log(f"  ashby/{slug}: {type(e).__name__} -- skipping")
            continue
        postings = data.get("jobs", [])
        for j in postings:
            comp = j.get("compensation") or {}
            jobs.append(make_job(
                source="ashby",
                source_id=j.get("id"),
                company=data.get("name") or slug.title(),
                title=j.get("title", ""),
                location=j.get("location", ""),
                url=j.get("jobUrl", ""),
                apply_url=j.get("applyUrl"),
                description=strip_html(j.get("descriptionHtml", "")),
                salary_raw=str(comp.get("compensationTierSummary") or "") or None,
                posted_at=j.get("publishedAt"),
            ))
        log(f"  ashby/{slug}: {len(postings)}")
    return jobs


def verify(slug: str) -> bool:
    try:
        get_json(BOARD.format(slug=slug))
        return True
    except Exception:
        return False
