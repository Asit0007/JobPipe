"""Parse LinkedIn / Naukri / Indeed job-alert emails out of your own inbox.

This is the ToS-clean route to the two boards you cannot touch directly. You
configure the saved searches on their site; they push results to you; we read
your mail. Zero account activity on either platform.

Link extraction is deterministic (regex on the anchor hrefs). Gemini is used
only to pull structured title/company pairs out of the surrounding text, which
varies wildly between senders and breaks constantly under pure regex.

Links are matched to postings BY POSITION IN THE TEXT, not by array index.
Pairing links[i] to jobs[i] assumes the regex and the model enumerate in the
same order; one interleaved promo link, or one posting the model merges or
drops, and every job after it silently gets someone else's URL -- which is
worse than having no URL at all.
"""
from __future__ import annotations

import re
from urllib.parse import (parse_qs, parse_qsl, unquote, urlencode,
                          urlparse, urlunparse)
from collections import Counter

from ..config import MODEL_SCORE, env, env_int
from ..llm import generate_json
from .base import make_job

# Match on the DOMAIN, not on individual From addresses. The boards send from
# whatever subdomain they like and change it without telling anyone: measured
# 2026-08-29, Indeed's alerts arrive from `donotreply@jobalert.indeed.com` and
# `donotreply@match.indeed.com`, while the two addresses previously listed here
# (`alert@` and `noreply@indeed.com`) matched **nothing at all**. That silently
# lost 21 of the 55 alert emails in a 3-day window.
#
# Checked against the user's hand-curated "Job Notifications" label, which is
# the ground truth for what an alert is: the domain-only form returns exactly
# those 55 -- nothing missed, nothing extra.
#
# Broad on purpose. A non-alert email that slips in costs one LLM call that
# finds no jobs; a missed sender loses the postings in silence. Same trade the
# alert prefilter carve-out makes in CLAUDE.md 3.
# MEASURED 2026-08-31, and this is why the OR is not a nicety:
#   label alone  -> 58 messages
#   domains alone-> 64
#   union        -> 64; the label caught 0 the domains missed, and the domains
#                   caught 6 the label missed -- EVERY LinkedIn alert in the
#                   window, because the user's Gmail filter does not route
#                   linkedin.com into the label.
# A bare `label:` query would have silently zeroed the LinkedIn channel, which
# is the highest-headroom source in the plan.
#
# The label is OR'd with the domain list rather than replacing it, so the
# query FAILS OPEN in both directions: a portal whose domain is not listed is
# still caught once you file it under the label, and a portal you forget to
# label is still caught by its domain. Setting GMAIL_ALERT_QUERY to a bare
# `label:...` is the one way to lose mail here -- an alert that misses the
# label becomes invisible, and this channel's every bug (7.2, 7.25, 7.34,
# 7.38) has been "produced nothing, reported success".
DEFAULT_QUERY = (
    'newer_than:3d ('
    'label:"Job Notifications" OR '
    'from:linkedin.com OR from:naukri.com OR '
    'from:indeed.com OR from:glassdoor.com OR from:glassdoor.co.in OR '
    'from:foundit.in OR from:instahyre.com OR from:cutshort.io OR '
    'from:hirist.tech OR from:hirist.com OR from:shine.com OR '
    'from:timesjobs.com OR from:wellfound.com OR from:talent500.co'
    ')'
)

# Override in .env when you file alerts under a label -- a hand-maintained
# label beats any sender list, because you curate it and the boards cannot
# break it by changing a subdomain. Quote it if the name has a space:
#   GMAIL_ALERT_QUERY=label:"Job Notifications" newer_than:3d
QUERY = env("GMAIL_ALERT_QUERY", DEFAULT_QUERY)

LINK_RE = re.compile(r'https?://[^\s"\'<>)]+', re.I)
# Matched against a LOWERCASED url -- keep every hint lowercase. Glassdoor's
# real link is `glassdoor.co.in/partner/jobListing.htm`, with a capital L, and
# the list previously carried only the lowercase `.com` spelling: wrong case and
# wrong TLD, so every Glassdoor link was skipped. The ccTLD is the same trap
# that 17c15aa fixed for SKIP_HOSTS.
JOB_LINK_HINTS = ("linkedin.com/jobs/view", "naukri.com/job-listings", "indeed.com/rc/clk",
                  "indeed.com/viewjob", "linkedin.com/comm/jobs/view",
                  "glassdoor.com/job-listing", "glassdoor.co.in/job-listing",
                  "glassdoor.com/partner/joblisting",
                  "glassdoor.co.in/partner/joblisting",
                  # The UNWRAPPED foundit target -- see _unwrap. Measured off a
                  # real alert on 2026-09-13, never guessed (7.35).
                  "foundit.in/job/")

# How far from a title a link may sit and still be considered its link. Anchor
# markup puts the href just before the visible text, so the window is asymmetric
# in practice but a symmetric bound is easier to reason about.
# Sender substring -> board name, first match wins. A row lands as
# `alert:<board>`, so an unmapped portal reports as "email" and its per-source
# yield is invisible. Measured 2026-09-01: Wellfound was already mailing from
# `team@hi.wellfound.com` and 9 of its postings were being dropped under the
# anonymous "email" label.
BOARDS = (
    ("linkedin", "linkedin"), ("naukri", "naukri"), ("indeed", "indeed"),
    ("glassdoor", "glassdoor"), ("wellfound", "wellfound"), ("foundit", "foundit"),
    ("instahyre", "instahyre"), ("cutshort", "cutshort"), ("hirist", "hirist"),
    ("shine", "shine"), ("timesjobs", "timesjobs"), ("talent500", "talent500"),
)

MAX_LINK_DISTANCE = 2000


def link_hints() -> tuple[str, ...]:
    """JOB_LINK_HINTS plus anything measured since the last release.

    A posting whose link matches no hint is DROPPED, so every new portal
    yields exactly zero rows until its host is known here. The hint has to be
    read off a real alert email and lowercased -- never guessed. Glassdoor's
    real link was `glassdoor.co.in/partner/jobListing.htm`, wrong case AND
    wrong TLD against what this tuple held, and every Glassdoor link was
    skipped for weeks (7.35).

    Read from the environment on each call so adding a measured hint is a
    .env line rather than a code change:
        GMAIL_EXTRA_LINK_HINTS=foundit.in/job-detail,instahyre.com/jobs
    """
    extra = env("GMAIL_EXTRA_LINK_HINTS", "") or ""
    return JOB_LINK_HINTS + tuple(
        h.strip().lower() for h in extra.split(",") if h.strip()
    )

PROMPT = """Extract every distinct job posting from this job-alert email.

Return JSON: {{"jobs":[{{"title":"...","company":"...","location":"...","snippet":"..."}}]}}

Rules:
- One entry per posting. Do not invent postings that are not present.
- Copy the title EXACTLY as it appears in the email, character for character.
  It is used to locate the posting's link, so a paraphrase breaks the match.
- If a field is absent, use an empty string. Do not guess.
- Ignore promotional content, footers, and "jobs you may be interested in" upsells.
- "snippet" is whatever description, blurb, requirement list or skill tags the
  email shows for THAT posting. Copy it VERBATIM from the email. Do not
  summarise it, do not tidy it up, and do not write one from the job title --
  this text is scored as if it were the job description, so an invented
  snippet produces an invented score. If the email shows no description for a
  posting, return "" and let the posting page supply it later.

EMAIL:
{body}
"""


# Portals that mail a tracking or auto-login wrapper with the real posting URL
# carried inside a query parameter. Measured 2026-09-13 on foundit (ex-Monster
# India), whose every link is:
#
#   foundit.in/rio/autoLogin/seeker/<base64>?return_url=https%3A%2F%2F...%2Fjob%2F66795517
#
# Unwrapping matters for two separate reasons, and the second is the important
# one:
#
#   * the real URL only exists percent-encoded, so NO plain hint can match it
#     and every foundit posting was dropped -- 10 emails on 09-11, 7 more on
#     09-13, all reported as "no recognisable posting link";
#   * that wrapper is an AUTO-LOGIN MAGIC LINK for the user's account. Matching
#     on it would store a working credential in jobs.apply_url, in every
#     prepared .md, and in the encrypted static export. A job link must never
#     be a session token.
_WRAPPED_PARAMS = ("return_url", "returnurl", "redirect", "redirect_uri", "url", "target")


# Parameters that ask the PORTAL to act rather than just display. foundit
# mails ...&autoApply=true, and §2 is the one rule in this codebase that
# overrides everything: nothing here may submit an application. A stored link
# that could submit on click is that rule failing through the one door left
# open -- the human's own browser. Stripped from every extracted link.
_ACTION_PARAMS = ("autoapply", "auto_apply", "applynow", "apply_now", "quickapply")


def _disarm(url: str) -> str:
    """Drop query parameters that could make the portal apply on our behalf."""
    try:
        parts = urlparse(url)
        kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                if k.lower() not in _ACTION_PARAMS]
    except ValueError:
        return url
    return urlunparse(parts._replace(query=urlencode(kept)))


def _unwrap(url: str) -> str:
    """The real destination of a tracking/auto-login wrapper, else the url.

    Only ever unwraps to an http(s) URL, and only one level: a parameter that
    holds something else is left alone rather than guessed at. The result is
    disarmed either way -- an un-wrapped link can carry an action param too.
    """
    try:
        q = parse_qs(urlparse(url).query)
    except ValueError:
        return url
    for key in _WRAPPED_PARAMS:
        for value in q.get(key, []):
            inner = unquote(value).strip()
            if inner.lower().startswith(("http://", "https://")):
                return _disarm(inner)
    return _disarm(url)


def _job_links(text: str) -> list[tuple[int, str]]:
    """Every posting link, with where it sits in the text. Order preserved.

    The POSITION is the raw match's, not the unwrapped URL's -- _match_link
    pairs a title to a link by distance in the email text (7.3), so the offset
    has to stay the one the text actually has.
    """
    out = []
    for m in LINK_RE.finditer(text):
        url = _unwrap(m.group(0))
        if any(h in url.lower() for h in link_hints()):
            out.append((m.start(), url))
    return out


# Glassdoor renders a posting heading as "<Company> <rating> * <Job title>",
# so the title the model copies back carries the company name and a star
# rating. Measured 2026-09-06: 20 rows, every one of them alert:glassdoor.
#
# It is deliberately NOT fixed in the prompt. The prompt asks for the title
# verbatim because _match_link locates the posting's link by finding that exact
# string in the email text (7.3), and only the verbatim string satisfies that
# contract by construction. Stripping the chrome first would happen to work
# while it is a prefix -- the remainder is still a substring of the email -- but
# that is luck, not a guarantee, and a posting with no confident link is dropped
# outright (7.3). So: match on the raw title, store the cleaned one.
_STAR_HEADING = re.compile(r"^.*?\u2605\s*")


def _clean_title(title: str) -> str:
    """Strip Glassdoor's "<Company> <rating> *" prefix off a posting heading.

    Not cosmetic. The polluted string is what `fingerprint()` hashes, so the
    same Glassdoor job arriving twice -- once with the chrome, once without --
    produced two rows and two prepared documents (observed live: same board, same
    city). It is also what `title_reject` and the title-only `soft_penalty`
    match against, and 7.18 moved those to the title precisely so that body
    text could not trigger them; a company name sitting in the title reopens
    that door. And it reaches the prepared document's header.
    """
    cleaned = _STAR_HEADING.sub("", title, count=1).strip()
    # A heading that is nothing but chrome is not something we can improve on.
    # Keep the original so a human sees the oddity rather than an empty title.
    return cleaned or title.strip()


def _match_link(title: str, text: str, links: list[tuple[int, str]],
                claimed: set[str]) -> str:
    """The unclaimed posting link nearest to where this title appears."""
    if not links or not title:
        return ""
    pos = text.lower().find(title.lower().strip())
    if pos < 0:
        return ""                       # model paraphrased; refuse to guess
    candidates = [(abs(lp - pos), url) for lp, url in links if url not in claimed]
    if not candidates:
        return ""
    distance, url = min(candidates)
    return url if distance <= MAX_LINK_DISTANCE else ""


def fetch(log=print) -> list[dict]:
    try:
        from ..gmail import body_text, headers, search
        # Measured 2026-08-31: 62 alert emails arrived in a 3-day window while
        # this was a hardcoded 25, and _search_imap keeps uids[-max_results:]
        # -- the NEWEST 25. So 37 were discarded per run with nothing logged,
        # and because Glassdoor sends over half the volume, the mail being
        # evicted was LinkedIn's and Naukri's. Configurable, and the run says
        # so when it hits the cap.
        cap = env_int("GMAIL_ALERT_MAX", 60)
        msgs = search(QUERY, max_results=cap)
    except Exception as e:
        log(f"  gmail_alert: {type(e).__name__} -- skipping ({e})")
        return []

    jobs: list[dict] = []
    # Keyed by board, not a bare total: "23 dropped" cannot tell you WHICH
    # portal is silent, and a portal that appears in the inbox but never in
    # the database is the signature of a missing link hint.
    unmatched: Counter[str] = Counter()
    # Two different faults with two different fixes, and conflating them sends
    # you to the wrong one. Measured 2026-09-01: 88 of 97 drops were on boards
    # whose hints ALREADY existed, while the log told you to add a hint --
    # 7.32's lesson (a confidently wrong diagnostic is worse than none) in code
    # written the day before.
    no_links: Counter[str] = Counter()
    for msg in msgs:
        text = body_text(msg)
        if not text:
            continue
        sender = headers(msg).get("from", "")
        sender_l = sender.lower()
        board = next((b for token, b in BOARDS if token in sender_l), "email")

        links = _job_links(text)
        if not links:
            no_links[board] += 1

        try:
            parsed = generate_json(
                PROMPT.format(body=text[:12000]), model=MODEL_SCORE, temperature=0.0
            )
        except Exception as e:
            log(f"  gmail_alert: parse failed ({type(e).__name__})")
            continue

        claimed: set[str] = set()
        for j in parsed.get("jobs", []):
            if not j.get("title") or not j.get("company"):
                continue
            url = _match_link(j["title"], text, links, claimed)
            if not url:
                # No confident link. Dropping it beats attaching the wrong one:
                # a wrong URL sends you to apply for somebody else's job.
                unmatched[board] += 1
                continue
            claimed.add(url)
            jobs.append(make_job(
                source=f"alert:{board}",
                source_id=None,
                company=j["company"],
                # Raw title above for _match_link, cleaned title here for
                # storage: the link match needs the verbatim string, the row
                # must not keep the company name and star rating.
                title=_clean_title(j["title"]),
                location=j.get("location", ""),
                url=url,
                # Whatever blurb the email carried. Usually short and often
                # empty -- LinkedIn and Indeed give a line or two, Naukri gives
                # skill tags. It is provisional: jdfetch still re-fetches
                # anything under MIN_USEFUL_CHARS and overwrites this with the
                # real posting page, and upsert_job backfills over an empty
                # one. Storing it costs nothing and, for the boards jdfetch
                # cannot reach (LinkedIn, Indeed, Glassdoor), it is the only
                # description the row will ever have.
                description=(j.get("snippet") or "").strip(),
            ))

    at_cap = " -- AT THE CAP, raise GMAIL_ALERT_MAX" if len(msgs) >= cap else ""
    log(f"  gmail_alert: {len(jobs)} from {len(msgs)} of max {cap} emails{at_cap}")
    # A hint is missing only when the email carried NO recognisable link at all.
    for board, n in no_links.most_common():
        log(f"  gmail_alert: {n} email(s) from {board} carried no recognisable"
            f" posting link -- add its host to GMAIL_EXTRA_LINK_HINTS")
    # Links were present and the title could not be paired to one. Adding a hint
    # fixes nothing here; the model paraphrased the title, or no unclaimed link
    # sits within MAX_LINK_DISTANCE. Dropping is correct (7.3) -- a wrong URL
    # sends you to apply for somebody else's job.
    for board, n in unmatched.most_common():
        log(f"  gmail_alert: {n} posting(s) from {board} had links but no"
            f" confident title match -- dropped rather than guess")
    return jobs
