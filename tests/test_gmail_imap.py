"""The IMAP backend, which replaces an OAuth token that dies every 7 days.

`gmail.readonly` is a restricted scope; an External app in Testing has its
refresh tokens expired weekly, publishing needs a CASA assessment, and Internal
needs a Cloud Organization the personal mailbox is not in. An App Password has
none of those problems.

The risk in the switch is silent behaviour change: `headers()` and `body_text()`
used to receive Gmail API dicts and now receive `email.message.Message`. Every
downstream bug this project has had in the alert channel (7.2, 7.25, 7.34, 7.38)
was "the channel produced nothing and reported success", so these tests pin the
two shapes against each other rather than testing the new one alone.
"""
import base64
from email.message import EmailMessage

import pytest

from jobpipe import gmail


def _api_msg(headers_list, parts):
    """A Gmail API message dict, the shape the old backend returned."""
    return {"payload": {"headers": [{"name": k, "value": v} for k, v in headers_list],
                        "parts": [{"mimeType": m,
                                   "body": {"data": base64.urlsafe_b64encode(
                                       b.encode()).decode()}} for m, b in parts]}}


def _imap_msg(subject, sender, plain=None, html=None):
    m = EmailMessage()
    m["Subject"] = subject
    m["From"] = sender
    if plain is not None and html is not None:
        m.set_content(plain)
        m.add_alternative(html, subtype="html")
    elif html is not None:
        m.set_content(html, subtype="html")
    else:
        m.set_content(plain or "")
    return m


# --------------------------------------------------------------------------
# the two message shapes must behave identically
# --------------------------------------------------------------------------
def test_headers_are_lowercased_for_both_backends():
    api = gmail.headers(_api_msg([("From", "alert@indeed.com"), ("Subject", "Jobs")], []))
    imap = gmail.headers(_imap_msg("Jobs", "alert@indeed.com"))
    assert api["from"] == imap["from"] == "alert@indeed.com"
    assert api["subject"] == imap["subject"] == "Jobs"


def test_an_rfc2047_encoded_header_is_decoded():
    """The API returned headers already decoded; IMAP does not. Left encoded, a
    subject reaches track.py as "=?UTF-8?B?...?=" and its company-token matching
    silently stops matching anything."""
    m = EmailMessage()
    m["Subject"] = "=?UTF-8?B?U2VuaW9yIERldk9wcyDigJQgQmVuZ2FsdXJ1?="
    m["From"] = "jobs@example.invalid"
    assert gmail.headers(m)["subject"] == "Senior DevOps — Bengaluru"


def test_plain_text_wins_over_html_for_both_backends():
    api = gmail.body_text(_api_msg([], [("text/plain", "PLAIN BODY"),
                                        ("text/html", "<p>HTML BODY</p>")]))
    imap = gmail.body_text(_imap_msg("s", "f", plain="PLAIN BODY", html="<p>HTML BODY</p>"))
    assert "PLAIN BODY" in api and "PLAIN BODY" in imap
    assert "HTML BODY" not in imap


def test_an_html_only_message_is_converted_not_returned_raw():
    """7.34: alert mail is HTML-only in practice. Returning markup raw fed a
    Glassdoor <head> -- preload links and CSS -- to the model, which found no
    jobs and reported no error."""
    html = ("<head><style>.x{color:red}</style></head><body>"
            "<a href='https://example.invalid/jobs/view/1'>DevOps Engineer</a></body>")
    text = gmail.body_text(_imap_msg("s", "f", html=html))
    assert "color:red" not in text and "<a" not in text


def test_a_link_stays_next_to_its_anchor_text():
    """7.3: `_match_link` pairs a title to a URL by POSITION. A bare strip_html
    deletes <a href=...> and the URL is gone before the matcher runs."""
    html = "<a href='https://example.invalid/jobs/view/99'>Senior DevOps Engineer</a>"
    text = gmail.body_text(_imap_msg("s", "f", html=html))
    i_title = text.index("Senior DevOps Engineer")
    i_url = text.index("https://example.invalid/jobs/view/99")
    assert 0 < i_url - i_title < 40, text


def test_an_attachment_is_not_mistaken_for_the_body():
    m = EmailMessage()
    m["Subject"], m["From"] = "s", "f@example.invalid"
    m.set_content("REAL BODY")
    m.add_attachment(b"ATTACHED TEXT", maintype="text", subtype="plain",
                     filename="notes.txt")
    assert "ATTACHED TEXT" not in gmail.body_text(m)
    assert "REAL BODY" in gmail.body_text(m)


def test_a_non_utf8_charset_does_not_crash():
    m = EmailMessage()
    m["Subject"], m["From"] = "s", "f@example.invalid"
    m.set_content("Café Münchén", charset="iso-8859-1")
    assert "Caf" in gmail.body_text(m)


# --------------------------------------------------------------------------
# query handling
# --------------------------------------------------------------------------
def test_the_gmail_query_is_passed_through_verbatim():
    """Gmail's IMAP server implements X-GM-RAW, which takes Gmail search syntax.
    Hand-translating to RFC 3501 SEARCH would quietly change what matches --
    `newer_than:3d`, `label:"..."` and `-category:promotions` have no direct
    IMAP equivalents."""
    q = 'newer_than:3d (from:linkedin.com OR from:naukri.com)'
    assert gmail._quote(q) == f'"{q}"'


def test_a_query_containing_a_quote_is_escaped():
    assert gmail._quote('label:"Job Notifications"') == r'"label:\"Job Notifications\""'


@pytest.mark.parametrize("addr,pw,expected", [
    ("a@gmail.com", "abcd efgh ijkl mnop", ("a@gmail.com", "abcdefghijklmnop")),
    ("a@gmail.com", "  abcdefghijklmnop ", ("a@gmail.com", "abcdefghijklmnop")),
    ("", "abcd", None),
    ("a@gmail.com", "", None),
])
def test_app_password_spaces_are_stripped(monkeypatch, addr, pw, expected):
    """Google displays the password in groups of four and people paste the
    spaces in. It is accepted either way, so strip rather than let a correct
    password look wrong."""
    monkeypatch.setenv("GMAIL_ADDRESS", addr)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", pw)
    assert gmail._app_password() == expected


def test_the_backend_is_chosen_by_whether_an_app_password_exists(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "a@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcdefghijklmnop")
    assert gmail.backend() == "imap"
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "")
    assert gmail.backend() == "oauth"


def test_a_missing_app_password_names_the_fix(monkeypatch):
    """A wrong diagnostic is worse than none -- CLAUDE.md 7.32."""
    monkeypatch.setenv("GMAIL_ADDRESS", "")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "")
    with pytest.raises(gmail.GmailAuthError, match="apppasswords"):
        gmail.connect()
