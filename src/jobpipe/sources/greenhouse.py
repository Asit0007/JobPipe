"""Greenhouse public job board API. No auth, no key, no ToS problem."""
from __future__ import annotations

from ..config import companies
from .base import get_json, make_job, strip_html

BOARD = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"


def fetch(log=print) -> list[dict]:
    jobs: list[dict] = []
    for slug in companies().get("greenhouse", []) or []:
        try:
            data = get_json(BOARD.format(slug=slug))
        except Exception as e:
            log(f"  greenhouse/{slug}: {type(e).__name__} -- skipping")
            continue
        for j in data.get("jobs", []):
            jobs.append(make_job(
                source="greenhouse",
                source_id=j.get("id"),
                company=slug.replace("-", " ").title(),
                title=j.get("title", ""),
                location=(j.get("location") or {}).get("name", ""),
                url=j.get("absolute_url", ""),
                description=strip_html(j.get("content", "")),
                posted_at=j.get("updated_at"),
            ))
        log(f"  greenhouse/{slug}: {len(data.get('jobs', []))}")
    return jobs


def verify(slug: str) -> bool:
    try:
        get_json(BOARD.format(slug=slug))
        return True
    except Exception:
        return False
