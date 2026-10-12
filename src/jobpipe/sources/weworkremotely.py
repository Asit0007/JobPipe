"""We Work Remotely, from its public RSS feeds. No auth, no key.

WWR has no JSON API, but publishes RSS per category, and its robots.txt allows
everything outside account pages (checked 2026-10-11). Two feeds: the all-jobs
feed (89 items that day) and DevOps & Sysadmin (19), whose items do not all
appear in the first. Repeats across the two are dropped by guid here; the
fingerprint would catch them anyway, but that would count them as "already known".

Two quirks, both pinned in tests/test_feed_sources.py:
  * <title> is "Company: Job Title". There is no separate company field.
  * <region> is the hiring region ("Anywhere in the World", "North America
    Only"), and <country> is the company's headquarters, not where you may work.
    Location is built from the region so a US-only role says so in the queue.

Applying through WWR needs its paid "Job Seeker Pro" membership (seen 2026-10-12;
reviews describe a $2.95 trial that becomes $14.95/month on a 12-month term).
Reading a posting is still free. The owner keeps the search at $0, so `url` stays
the WWR posting and `apply_url` is a web search for the same role, to apply on
the employer's own site. The feed has no reliable employer apply link: of 112
items on 2026-10-12, most outbound links were privacy or legal pages.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from urllib.parse import quote_plus

from .base import get_text, make_job, strip_html

EMPLOYER_SEARCH = "https://www.google.com/search?q="


def employer_search(company: str, title: str) -> str:
    """A search for the employer's own posting of this role (WWR's apply is paid)."""
    return EMPLOYER_SEARCH + quote_plus(f'"{company}" "{title}" careers OR jobs')

FEEDS = (
    "https://weworkremotely.com/remote-jobs.rss",
    "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
)


def _text(item, tag: str) -> str:
    return (item.findtext(tag) or "").strip()


def parse(xml: str) -> list[dict]:
    jobs = []
    for item in ET.fromstring(xml).iter("item"):
        company, sep, title = _text(item, "title").partition(": ")
        if not (sep and company.strip() and title.strip()):
            continue
        region = _text(item, "region")
        link = _text(item, "guid") or _text(item, "link")
        description = strip_html(_text(item, "description"))
        if region:
            # The region is the one location fact WWR gives; keep it where the
            # prefilter's geography rules and the scorer will both read it.
            description = f"Hiring region: {region}\n\n{description}"
        jobs.append(make_job(
            source="weworkremotely",
            source_id=link.rstrip("/").rsplit("/", 1)[-1] or None,
            company=company.strip(),
            title=title.strip(),
            location=f"Remote ({region})" if region else "Remote",
            url=link,
            apply_url=employer_search(company.strip(), title.strip()),
            description=description,
            posted_at=_text(item, "pubDate") or None,
        ))
    return jobs


def fetch(log=print) -> list[dict]:
    jobs, seen = [], set()
    for feed in FEEDS:
        try:
            items = parse(get_text(feed))
        except Exception as e:
            log(f"  weworkremotely: {type(e).__name__} on {feed.rsplit('/', 1)[-1]} -- skipping feed")
            continue
        for j in items:
            if j["url"] not in seen:
                seen.add(j["url"])
                jobs.append(j)
    log(f"  weworkremotely: {len(jobs)}")
    return jobs
