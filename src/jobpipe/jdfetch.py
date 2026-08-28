"""Fetch job descriptions for postings that arrived without one.

Email alerts give a title, a company and a link -- no description. The keyword
prefilter then counts zero must-have terms and kills them for free, which is
why the LinkedIn/Naukri path used to produce nothing usable at all.

Scope, deliberately narrow:
  - GET the public posting page only. Never a login, never a session, never a
    form. If the page needs an account, we get the logged-out version or
    nothing, and nothing is an acceptable answer.
  - robots.txt is consulted per host and cached for the run. A Disallow is
    final -- we skip the row and leave it description-less rather than
    arguing with it.
  - One request at a time with a delay between them. This runs against a
    handful of rows a day; there is no reason to be fast.
"""
from __future__ import annotations

import re
import time
import urllib.robotparser
from urllib.parse import urlparse

import httpx

from . import db
from .config import env_bool, env_int
from .sources.base import UA, strip_html

DELAY_SECONDS = 1.5
MIN_USEFUL_CHARS = 400

# Hosts that serve a login wall or pure JS to a plain GET. Asking is a waste of
# a request and of their bandwidth, so we don't.
# "glassdoor." not "glassdoor.com": the match is a substring of the netloc, so
# the .com form silently let www.glassdoor.co.in through -- an Indian Glassdoor
# link would spend a request to be handed the same login wall.
#
# "adzuna." earns its place differently. Adzuna's API hands back a redirect
# landing page (adzuna.in/land/ad/NNNN) and never the employer's own URL, and
# that page answers 403 to a plain GET -- measured on four rows, all identical.
# Its descriptions are truncated to ~500 chars, which sits ABOVE
# MIN_USEFUL_CHARS, so these rows were never selected for a re-fetch anyway and
# nothing was being wasted. Raising the threshold to catch them, which looks
# like the obvious fix for 7.15, would instead buy ~374 requests that all 403.
# The entry makes that explicit so the next person does not try it.
SKIP_HOSTS = ("linkedin.com", "indeed.com", "glassdoor.", "ziprecruiter.com",
              "adzuna.")

_robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}


def _allowed(url: str, client: httpx.Client) -> bool:
    host = urlparse(url).netloc.lower()
    if any(h in host for h in SKIP_HOSTS):
        return False
    if host not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = client.get(f"{urlparse(url).scheme}://{host}/robots.txt", timeout=10)
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
            else:
                rp = None          # no robots.txt published == no restriction
        except httpx.RequestError:
            rp = None
        _robots_cache[host] = rp
    rp = _robots_cache[host]
    return True if rp is None else rp.can_fetch(UA["User-Agent"], url)


def _extract(html: str) -> str:
    """Pull the body text, preferring a JSON-LD JobPosting when one exists.

    Most ATS pages publish schema.org/JobPosting for Google Jobs. When they do
    it is far cleaner than scraping the rendered page.
    """
    m = re.search(
        r'"description"\s*:\s*"((?:[^"\\]|\\.)*)"', html, re.I
    ) if "JobPosting" in html else None
    if m:
        try:
            text = strip_html(m.group(1).encode().decode("unicode_escape"))
            if len(text) >= MIN_USEFUL_CHARS:
                return text
        except (UnicodeDecodeError, ValueError):
            pass

    body = re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", html)
    return strip_html(body)


def run(log=print, limit: int | None = None) -> int:
    """Fill in descriptions for rows that have none. Returns rows enriched."""
    if not env_bool("JD_FETCH_ENABLED", True):
        log("  jd fetch: disabled (JD_FETCH_ENABLED=false)")
        return 0

    cap = limit if limit is not None else env_int("JD_FETCH_MAX", 25)
    with db.connect() as c:
        rows = c.execute(
            "SELECT id, title, company, url, apply_url FROM jobs "
            "WHERE status = 'discovered' "
            "AND (description IS NULL OR length(trim(description)) < ?) "
            "ORDER BY first_seen DESC LIMIT ?",
            (MIN_USEFUL_CHARS, cap),
        ).fetchall()

    if not rows:
        return 0

    log(f"  jd fetch: {len(rows)} posting(s) missing a description")
    filled = skipped = 0
    with httpx.Client(headers=UA, follow_redirects=True, timeout=20) as client:
        for row in rows:
            url = row["url"] or row["apply_url"]
            if not url:
                continue
            if not _allowed(url, client):
                skipped += 1
                continue
            try:
                r = client.get(url)
                r.raise_for_status()
            except (httpx.HTTPError, httpx.RequestError):
                skipped += 1
                time.sleep(DELAY_SECONDS)
                continue

            text = _extract(r.text)
            if len(text) >= MIN_USEFUL_CHARS:
                db.update(row["id"], description=text[:20000])
                filled += 1
            else:
                skipped += 1
            time.sleep(DELAY_SECONDS)

    log(f"  jd fetch: {filled} filled, {skipped} unavailable")
    return filled
