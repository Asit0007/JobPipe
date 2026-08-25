"""Adzuna aggregator. Free tier: 1000 calls/month. Covers the India market."""
from __future__ import annotations

from ..config import env, profile
from .base import get_json, make_job, strip_html

URL = "https://api.adzuna.com/v1/api/jobs/in/search/{page}"


def fetch(log=print) -> list[dict]:
    app_id, app_key = env("ADZUNA_APP_ID"), env("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        log("  adzuna: no credentials, skipping")
        return []

    jobs: list[dict] = []
    for title in profile()["targets"]["titles"]:
        try:
            data = get_json(URL.format(page=1), params={
                "app_id": app_id, "app_key": app_key,
                "what": title, "where": "India",
                "results_per_page": 25, "max_days_old": 21,
                "content-type": "application/json",
            })
        except Exception as e:
            log(f"  adzuna/{title}: {type(e).__name__} -- skipping")
            continue
        for j in data.get("results", []):
            jobs.append(make_job(
                source="adzuna",
                source_id=j.get("id"),
                company=(j.get("company") or {}).get("display_name", "Unknown"),
                title=j.get("title", ""),
                location=(j.get("location") or {}).get("display_name", ""),
                url=j.get("redirect_url", ""),
                description=strip_html(j.get("description", "")),
                salary_raw=str(j.get("salary_min") or "") or None,
                posted_at=j.get("created"),
            ))
        log(f"  adzuna/{title}: {len(data.get('results', []))}")
    return jobs
