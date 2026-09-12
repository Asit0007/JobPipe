"""Canonicalisation and deduplication.

Staffing firms repost the same requisition under many titles and many job IDs.
Without this layer the DB fills with the same 30 roles wearing different hats.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

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


# Same place, two spellings. Alert mail is the source that makes this matter --
# the same requisition arrives from Indeed as "Bengaluru, Karnataka" and from
# Glassdoor as "Bengaluru", and until 2026-09-12 those were two rows.
_CITY_ALIASES = (
    ("bengaluru", "bangalore"), ("gurugram", "gurgaon"), ("bombay", "mumbai"),
    ("calcutta", "kolkata"), ("madras", "chennai"), ("trivandrum", "thiruvananthapuram"),
    ("vizag", "visakhapatnam"), ("baroda", "vadodara"), ("poona", "pune"),
)

# Cities this search actually targets, AFTER aliasing. A location containing one
# of these collapses to it for fingerprinting; anything else is left alone. See
# canon_place() for why the list is a list and not a rule.
_KNOWN_CITIES = (
    "new delhi", "bangalore", "pune", "hyderabad", "chennai", "mumbai", "delhi",
    "noida", "gurgaon", "kolkata", "ahmedabad", "jaipur", "indore", "coimbatore",
    "kochi", "thiruvananthapuram", "bhubaneswar", "dehradun", "nagpur",
    "chandigarh", "mysore", "visakhapatnam", "vadodara", "surat", "lucknow",
    "bhopal", "nashik", "madurai",
)


def _strip_accents(s: str) -> str:
    """Hyderabad and Hyderab\u0101d are the same city.

    Measured 2026-09-12: Glassdoor mails the macron form, Indeed does not, and
    the pair produced two rows for one MetLife requisition.
    """
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def canon_location(loc: str) -> str:
    s = _strip_accents((loc or "").lower())
    for alias, canonical in _CITY_ALIASES:
        s = s.replace(alias, canonical)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def canon_place(loc: str) -> str:
    """The location key the fingerprint uses: a known city, else the string.

    The problem this solves is administrative-suffix variance -- one job
    reaching us as "Pune", "Pune, Maharashtra" and "Pune Division" from three
    boards, which the raw canonical string hashes into three rows. Measured
    2026-09-12: 30 such groups among the 502 rows that had reached the queue,
    including an `applied` Heaptrace role whose Glassdoor twin was still sitting
    `shortlisted` and would have spent tailor budget on a second document.

    **Not `_city()`, which looks like it would do.** That takes the leading
    token, which is right for the gate it was written for -- being wrong there
    errs toward "different" and keeps both rows. In a fingerprint the error
    reverses and silently merges: "San Francisco" and "San Diego" both lead with
    "san". A known-city list cannot make that mistake, because a city it does
    not recognise falls through unchanged. Measured, that is the difference
    between merging 103 rows corpus-wide and merging 377.

    Country-only locations are deliberately NOT folded into a city. "India" and
    "Pune" stay two rows even for one company and title -- a nationwide or
    remote posting is not the Pune one, and 7.4 is the record of what
    over-eager location merging costs.
    """
    c = canon_location(loc)
    for city in _KNOWN_CITIES:
        if re.search(rf"\b{re.escape(city)}\b", c):
            return city
    return c[:20]


def _city(loc: str) -> str:
    """First token of a canonicalised location -- near enough to the city.

    canon_location keeps the state suffix, so "Bengaluru" and "Bangalore, KA"
    canonicalise to different strings despite being the same place. Comparing
    the leading token gets those right, and when it is wrong it errs toward
    "different", which keeps both postings rather than silently dropping one.
    """
    return canon_location(loc).split(" ")[0] if loc else ""


def fingerprint(company: str, title: str, location: str) -> str:
    key = f"{canon_company(company)}|{canon_title(title)}|{canon_place(location)}"
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


# An ATS board is authoritative: one row per open requisition, no reposts.
AUTHORITATIVE_SOURCES = ("greenhouse", "lever", "ashby")

# A staffing repost announces itself in the COMPANY name, or in client-speak no
# employer writes about itself. It does not announce itself with the bare word
# "recruitment": that lives in the EEO footer, the privacy notice and the
# anti-scam warning of nearly every large-company posting. Matched against the
# description it flagged 708 of 4,654 rows -- "Recruitment Fraud Alert" on
# Atlan, an IBM privacy notice on Confluent -- all of them first-party boards
# where a repost cannot exist. Same terms as before, routed to the right field.
STAFFING_COMPANY_TERMS = (
    "staffing", "recruitment", "manpower", "talent acquisition partner",
)
STAFFING_CLIENT_SPEAK = (
    "our client is", "on behalf of our client", "leading mnc",
    "c2h", "contract to hire", "payroll of",
)


def looks_like_staffing_firm(company: str, description: str = "",
                             source: str = "") -> bool:
    """Not a rejection -- a flag. Consultancy reposts are noisy but not always bad."""
    if source in AUTHORITATIVE_SOURCES:
        return False
    if any(s in (company or "").lower() for s in STAFFING_COMPANY_TERMS):
        return True
    return any(s in (description or "").lower() for s in STAFFING_CLIENT_SPEAK)
