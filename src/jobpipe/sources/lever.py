"""Lever public postings API."""
from __future__ import annotations

from ..config import companies
from .base import get_json, make_job, strip_html

BOARD = "https://api.lever.co/v0/postings/{slug}?mode=json"


def fetch(log=print) -> list[dict]:
    jobs: list[dict] = []
    for slug in companies().get("lever", []) or []:
        try:
            data = get_json(BOARD.format(slug=slug))
        except Exception as e:
            log(f"  lever/{slug}: {type(e).__name__} -- skipping")
            continue
        for j in data:
            cats = j.get("categories") or {}
            jobs.append(make_job(
                source="lever",
                source_id=j.get("id"),
                company=slug.replace("-", " ").title(),
                title=j.get("text", ""),
                location=cats.get("location", ""),
                url=j.get("hostedUrl", ""),
                apply_url=j.get("applyUrl"),
                description=strip_html(j.get("descriptionPlain") or j.get("description", "")),
                posted_at=str(j.get("createdAt", "")),
            ))
        log(f"  lever/{slug}: {len(data)}")
    return jobs


def verify(slug: str) -> bool:
    try:
        get_json(BOARD.format(slug=slug))
        return True
    except Exception:
        return False
