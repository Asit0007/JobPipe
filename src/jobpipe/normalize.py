"""Canonicalisation and deduplication.

Staffing firms repost the same requisition under many titles and many job IDs.
Without this layer the DB fills with the same 30 roles wearing different hats.
"""
from __future__ import annotations

import hashlib
import re

from rapidfuzz import fuzz

_SUFFIXES = r"(pvt\.?|private|ltd\.?|limited|llp|inc\.?|corp\.?|corporation|technologies|technology|solutions|services|systems|india|global|consulting)"

_SENIORITY = r"(sr\.?|senior|jr\.?|junior|lead|staff|principal|associate|i{1,3}\b|[1-3]\b)"

_NOISE = r"(urgent|immediate joiner|hiring|walk-?in|wfh|work from home|remote|contract|c2h|full[- ]time|\d+\s*-?\s*\d*\s*(yrs?|years?))"


def canon_company(name: str) -> str:
    s = (name or "").lower().strip()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(_SUFFIXES, " ", s)
    return re.sub(r"\s+", " ", s).strip()


def canon_title(title: str) -> str:
    s = (title or "").lower().strip()
    s = re.sub(r"[^\w\s/+]", " ", s)
    s = re.sub(_NOISE, " ", s)
    s = re.sub(_SENIORITY, " ", s)
    return re.sub(r"\s+", " ", s).strip()


def canon_location(loc: str) -> str:
    s = (loc or "").lower()
    s = s.replace("bengaluru", "bangalore").replace("gurugram", "gurgaon")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _city(loc: str) -> str:
    """First token of a canonicalised location -- near enough to the city.

    canon_location keeps the state suffix, so "Bengaluru" and "Bangalore, KA"
    canonicalise to different strings despite being the same place. Comparing
    the leading token gets those right, and when it is wrong it errs toward
    "different", which keeps both postings rather than silently dropping one.
    """
    return canon_location(loc).split(" ")[0] if loc else ""


def fingerprint(company: str, title: str, location: str) -> str:
    key = f"{canon_company(company)}|{canon_title(title)}|{canon_location(location)[:20]}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def is_near_duplicate(a: dict, b: dict, threshold: int = 88) -> bool:
    """Catches what the hash misses: same role, slightly different wording.

    Location participates when both sides have one. Without that gate this
    folds a Toronto posting into a New York one purely because the titles
    rhyme -- and canon_title has already stripped the seniority, so
    "Senior SRE" and "SRE" at one company collapse into a single row. At a
    consultancy reposting the same requisition that is the point; at a real
    employer those are two jobs you could apply to separately.
    """
    if canon_company(a["company"]) != canon_company(b["company"]):
        return False
    la, lb = _city(a.get("location", "")), _city(b.get("location", ""))
    if la and lb and la != lb:
        return False
    return fuzz.token_sort_ratio(canon_title(a["title"]), canon_title(b["title"])) >= threshold


def looks_like_staffing_firm(company: str, description: str = "") -> bool:
    """Not a rejection -- a flag. Consultancy reposts are noisy but not always bad."""
    blob = f"{company} {description}".lower()
    signals = [
        "staffing", "recruitment", "manpower", "talent acquisition partner",
        "our client is", "on behalf of our client", "leading mnc",
        "c2h", "contract to hire", "payroll of",
    ]
    return any(s in blob for s in signals)
