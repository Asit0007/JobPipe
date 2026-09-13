"""Portals that wrap the real posting URL in a redirect parameter.

Measured 2026-09-13 on foundit (ex-Monster India). Every link it mails is

    foundit.in/rio/autoLogin/seeker/<base64>?return_url=<encoded real url>

so the real URL exists only percent-encoded and no plain hint could match it:
10 emails on 09-11 and 7 more on 09-13 were reported as "no recognisable
posting link" and dropped whole.
"""
from urllib.parse import parse_qs, urlparse

import pytest

from jobpipe.sources.gmail_alerts import _disarm, _job_links, _unwrap

WRAPPED = ("https://www.foundit.in/rio/autoLogin/seeker/WmIxcnRUbm5p"
           "?return_url=https%3A%2F%2Fwww.foundit.in%2Fjob%2F66795517"
           "%3FautoApply%3Dtrue%26spl%3Dmonsterindia")


def test_the_real_posting_url_is_recovered():
    assert _unwrap(WRAPPED).startswith("https://www.foundit.in/job/66795517")


def test_the_autologin_token_does_NOT_survive():
    """That wrapper is a magic-link credential for the user's account.

    Matching on it would store a working login in jobs.apply_url, in every
    prepared .md, and in the encrypted static export.
    """
    assert "autologin" not in _unwrap(WRAPPED).lower()
    assert "WmIxcnRUbm5p" not in _unwrap(WRAPPED)


def test_an_auto_apply_parameter_is_stripped():
    """Bug class, not cosmetics: §2 says nothing here may submit an application.

    A stored link carrying autoApply=true is that rule failing through the one
    door left open -- the human's own browser.
    """
    assert "autoapply" not in _unwrap(WRAPPED).lower()
    assert "autoapply" not in _disarm(
        "https://x.example/job/9?autoApply=true").lower()


def test_stripping_keeps_the_other_parameters():
    out = _unwrap(WRAPPED)
    assert parse_qs(urlparse(out).query)["spl"] == ["monsterindia"]


@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/jobs/view/12345",
    "https://in.indeed.com/viewjob?jk=abc123",
])
def test_an_ordinary_link_is_left_alone(url):
    assert _unwrap(url) == url


def test_a_non_http_redirect_target_is_not_followed():
    """A relative or javascript: value must not become the posting URL."""
    for bad in ("%2Flocal%2Fpath", "javascript%3Aalert(1)", "mailto%3Aa%40b.c"):
        u = f"https://x.example/go?return_url={bad}"
        assert _unwrap(u).startswith("https://x.example/go")


def test_the_link_POSITION_stays_that_of_the_raw_match():
    """_match_link pairs a title to a link by distance in the text (7.3).

    Unwrapping changes the string; it must not change where the link sits, or
    every pairing shifts.
    """
    text = ("Cloud Engineer\n" + WRAPPED + "\nSome other line")
    links = _job_links(text)
    assert links, "the foundit link should now be recognised"
    offset, url = links[0]
    assert text[offset:offset + 20] == WRAPPED[:20], "offset must index the RAW url"
    assert "/job/66795517" in url


def test_foundit_links_are_recognised_at_all():
    """Before the hint + unwrap, _job_links returned nothing for foundit."""
    assert _job_links(WRAPPED)
