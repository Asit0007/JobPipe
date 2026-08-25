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


def fingerprint(company: str, title: str, location: str) -> str:
    key = f"{canon_company(company)}|{canon_title(title)}|{canon_location(location)[:20]}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def is_near_duplicate(a: dict, b: dict, threshold: int = 88) -> bool:
    """Catches what the hash misses: same role, slightly different wording."""
    if canon_company(a["company"]) != canon_company(b["company"]):
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
