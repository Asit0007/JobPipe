"""Alert email is HTML-only, and that broke the whole channel silently.

Measured 2026-08-29 against the real inbox: all 25 job-alert emails carried a
single `text/html` part and no `text/plain` at all. `body_text()` returned that
markup raw, `gmail_alerts.fetch()` truncated it at 12,000 chars, and on a
Glassdoor alert those 12,000 characters are `<head>` -- preload links and CSS.
The model was asked to find jobs in a stylesheet, found none, and the run
reported "0 from 25 emails" with no error, because finding nothing is not a
failure. Same family as 7.2 and 7.25: the alert channel producing nothing while
looking healthy.
"""
import base64

from jobpipe.gmail import body_text
from jobpipe.sources.gmail_alerts import _job_links

GLASSDOOR_HREF = (
    "https://www.glassdoor.co.in/partner/jobListing.htm"
    "?pos=101&ao=1136043&s=58&guid=000001a04d40&src=GD_JOB_AD"
)

# Shaped like the real thing: a large <head>, zero-width preheader padding, and
# the jobs far enough down that a naive [:12000] slice never reaches them.
HTML = (
    "<!DOCTYPE html><html><head>"
    + '<link rel="preload" as="image" href="https://x/logo.png">' * 300
    + "<style>.a{color:red}</style></head><body>"
    + "‌​‍‎‏﻿" * 150
    + "<div>Job alert: IT Systems Administrator</div>"
    + f'<a href="{GLASSDOOR_HREF}">Site Reliability Engineer</a>'
    + "<div>Aldaa</div><div>Remote, India</div>"
    + "</body></html>"
)


def _msg(mime_parts):
    """A Gmail API message with the given [(mimeType, text)] parts."""
    def part(mime, text):
        return {"mimeType": mime, "body": {
            "data": base64.urlsafe_b64encode(text.encode()).decode()}}
    if len(mime_parts) == 1:
        return {"payload": part(*mime_parts[0])}
    return {"payload": {"mimeType": "multipart/alternative",
                        "body": {}, "parts": [part(m, t) for m, t in mime_parts]}}


def test_html_only_mail_becomes_readable_text():
    text = body_text(_msg([("text/html", HTML)]))
    assert "<link" not in text and "<style" not in text, "markup survived"
    assert "Site Reliability Engineer" in text
    assert "Aldaa" in text


def test_the_job_survives_the_12000_char_truncation():
    """The actual bug. Raw, the jobs sit past 12,000 chars behind the <head>."""
    assert "Site Reliability Engineer" not in HTML[:12000], "fixture is not representative"
    text = body_text(_msg([("text/html", HTML)]))
    assert "Site Reliability Engineer" in text[:12000]


def test_zero_width_preheader_padding_is_dropped():
    text = body_text(_msg([("text/html", HTML)]))
    assert "‌" not in text and "﻿" not in text


def test_links_survive_as_bare_urls_next_to_their_title():
    """Wrapping the href in <> is self-defeating: strip_html deletes anything
    matching <[^>]+> immediately afterwards. Bare, and adjacent, because
    `_match_link` pairs a title to a URL by POSITION (7.3)."""
    text = body_text(_msg([("text/html", HTML)]))
    assert GLASSDOOR_HREF in text
    title_at = text.index("Site Reliability Engineer")
    link_at = text.index(GLASSDOOR_HREF)
    assert abs(link_at - title_at) < 200, "link drifted away from its title"


def test_text_plain_is_preferred_when_present():
    msg = _msg([("text/plain", "plain wins"), ("text/html", HTML)])
    assert body_text(msg).strip() == "plain wins"


# --- the hint list ----------------------------------------------------------

def test_glassdoor_ccTLD_and_capital_L_are_matched():
    """`glassdoor.co.in/partner/jobListing.htm` missed on BOTH counts: the hint
    list held only the lowercase `.com` spelling, and matching was
    case-sensitive. Every Glassdoor link was skipped."""
    found = _job_links(f"Some Title {GLASSDOOR_HREF} more text")
    assert len(found) == 1, "the real Glassdoor link must be recognised"
    assert found[0][1] == GLASSDOOR_HREF


def test_hint_list_is_all_lowercase():
    """Matching lowercases the url, so an uppercase hint could never fire."""
    from jobpipe.sources.gmail_alerts import JOB_LINK_HINTS
    assert all(h == h.lower() for h in JOB_LINK_HINTS)


def test_a_non_posting_link_is_still_ignored():
    assert _job_links("Title https://www.glassdoor.co.in/about-us") == []


# --- the alert search query -------------------------------------------------

def test_query_matches_domains_not_individual_senders():
    """Measured 2026-08-29: Indeed's alerts arrive from
    `donotreply@jobalert.indeed.com` and `donotreply@match.indeed.com`, while
    the addresses the query used to list (`alert@` / `noreply@indeed.com`)
    matched nothing at all -- 21 of 55 alert emails lost in silence. Boards
    change subdomains without notice, so match the domain."""
    from jobpipe.sources.gmail_alerts import DEFAULT_QUERY
    for domain in ("linkedin.com", "naukri.com", "indeed.com", "glassdoor.com"):
        assert f"from:{domain}" in DEFAULT_QUERY, f"{domain} must match at the domain"
    # An address-scoped sender would re-introduce the bug.
    for addr in ("alert@indeed.com", "noreply@indeed.com",
                 "info@naukri.com", "jobalerts-noreply@linkedin.com"):
        assert addr not in DEFAULT_QUERY, f"{addr} is too narrow to rely on"


def test_query_is_overridable_from_env(monkeypatch):
    """A hand-curated label beats any sender list: the user maintains it and no
    board can break it by changing a subdomain."""
    import importlib
    from jobpipe.sources import gmail_alerts
    monkeypatch.setenv("GMAIL_ALERT_QUERY", 'label:"Job Notifications" newer_than:3d')
    reloaded = importlib.reload(gmail_alerts)
    try:
        assert reloaded.QUERY == 'label:"Job Notifications" newer_than:3d'
    finally:
        monkeypatch.undo()
        importlib.reload(gmail_alerts)
