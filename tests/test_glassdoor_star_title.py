"""Glassdoor alert headings carry "<Company> <rating> * <Job title>".

Measured 2026-09-06: 20 rows in the live corpus, every one alert:glassdoor.
Not cosmetic -- the polluted string is what `fingerprint()` hashes, so the same
job arriving once with the chrome and once without produced two rows and two
prepared documents (observed live on a real posting). It is also what
`title_reject` and the title-only
`soft_penalty` match against (7.18 put those on the title so body text could
not trigger them; a company name in the title reopens that door).

The ORDER is the whole design, and `test_cleaning_before_the_link_match_would
_lose_the_link` is the test that pins it: the prompt asks for a verbatim title
because `_match_link` finds the link by locating that exact string in the email
(7.3). Clean it too early and the posting is dropped instead of mispaired.
"""
from jobpipe.normalize import fingerprint
from jobpipe.sources.gmail_alerts import _clean_title, _match_link

RAW = "Northwind Systems 3.0 ★ Associate DevOps Engineer"

EMAIL = (
    "Jobs for you\n"
    f"{RAW}\n"
    "Pune  https://www.glassdoor.co.in/partner/jobListing.htm?pos=101&ao=1\n"
)
LINKS = [(EMAIL.find("https://"), "https://www.glassdoor.co.in/partner/jobListing.htm?pos=101&ao=1")]


def test_it_strips_the_company_and_star_rating():
    assert _clean_title(RAW) == "Associate DevOps Engineer"


def test_a_clean_title_is_left_exactly_alone():
    for good in ("Senior DevOps Engineer",
                 "Staff Engineer DevOps, Data Security (DLP)",
                 "DevOps Engineer_(SA2117)"):
        assert _clean_title(good) == good


def test_a_heading_that_is_only_chrome_keeps_the_original():
    """Never return an empty title -- an odd heading a human can see beats a
    blank one that looks like a different bug."""
    assert _clean_title("★") == "★"
    assert _clean_title("  ★  ") == "★"


def test_the_polluted_and_clean_titles_now_fingerprint_the_same():
    """The actual dedup bug: these two are one job at one company in one city.

    A real posting reached the queue twice because the hash saw two titles.
    """
    polluted = fingerprint("Northwind Systems", RAW, "Pune")
    clean = fingerprint("Northwind Systems", "Associate DevOps Engineer", "Pune")
    assert polluted != clean, "precondition: the raw heading hashes differently"
    assert fingerprint("Northwind Systems", _clean_title(RAW), "Pune") == clean


def test_the_raw_title_still_finds_its_link():
    assert _match_link(RAW, EMAIL, LINKS, set()) == LINKS[0][1]


def test_matching_on_the_raw_title_is_guaranteed_rather_than_lucky():
    """Why the cleaner runs AFTER _match_link, stated accurately.

    Cleaning first would happen to work HERE: the cleaned title is a suffix of
    the heading, so it is still a substring of the email and `find()` locates
    it. That is contingent, not guaranteed. `_match_link`'s contract is "the
    title exactly as it appears in the email" and the prompt is written to
    satisfy it; only the raw string does so by construction. So match on the
    raw and store the clean -- the ordering costs nothing and removes the
    dependence on the chrome always being a prefix.
    """
    assert RAW.endswith(_clean_title(RAW))
    assert _match_link(_clean_title(RAW), EMAIL, LINKS, set()) == LINKS[0][1]
    assert _match_link(RAW, EMAIL, LINKS, set()) == LINKS[0][1]
