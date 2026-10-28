"""Reply tracking. Reads your inbox, tells you who wrote back, flags follow-ups.

Matching is the hard part and the old version got it wrong three ways:

  1. A substring test of a canonicalised company name against from+subject.
     Short names -- ramp, linear, vanta -- matched unrelated mail constantly.
  2. Applications were keyed by company, so two applications to the same
     company collided and only the last one could ever be tracked.
  3. Every message rescanned every application, O(n*m).

Now: candidates are indexed by job id, a match needs at least two independent
signals, and same-company applications are separated by title tokens.

Reading was wrong too (2026-10-27). It downloaded the newest 40 messages of
`newer_than:7d -category:promotions`, a query that matched 362 in the week
measured, almost all of them job alerts. The newest 40 reached back about 18
hours, so a reply that came earlier, or on a day the Mac was off, was never
read: over 60 days that lost three employers' status updates. Recruiters who
wrote without an application behind them were never looked for at all. Now
every run reads the headers of the whole window, sorts each new message into
one of three kinds, remembers it in the `replies` table so it is handled once,
and sends one Telegram message with whatever is new:

  application  mail about an application: an applicant-tracking sender, a
               match to a row you marked applied, or an application subject.
               The model reads it and answers interview / offer / recruiter /
               rejection, or noise for an acknowledgement, which is never sent.
  recruiter    a person writing to your Primary tab in recruiter words
               ("notice period", CTC, interview, your profile ...), or a named
               person writing in those words from their own mailbox to any
               other tab (a staffing firm's recruiter landed in Promotions).
               Shown to no model, because the Primary tab is also where
               personal mail is.
  portal       a person writing through a job board: a recruiter's Naukri
               mail, a Cutshort reply, a LinkedIn message.
  invite       a board's own mail saying a company reached out: a foundit
               invitation to apply, a Shine shortlist or recruiter contact, an
               employer's Cutshort message you have not answered, Naukri's
               "recruiters expressed interest". One line per board.

The last two widenings (owner, 2026-10-27: "notify me whenever any company or
HR reaches out") were measured on 60 days of mail before they were written:
see _PORTAL_OUTREACH and _person_sender.
"""
from __future__ import annotations

import hashlib
import html
import re
from collections import Counter, defaultdict
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from typing import NoReturn
from urllib.parse import urlparse

from . import db, notify
from .config import MODEL_SCORE, env
from .db import now
from .llm import generate_json
from .normalize import canon_company

PROMPT = """Classify this email from a company you applied to.

Return JSON: {{"kind":"rejection"|"recruiter"|"interview"|"offer"|"noise","company":"<company name or empty>"}}

- "recruiter" = a human asking to talk or requesting details
- "interview" = a scheduled round or an assessment link
- "noise"     = newsletters, job alerts, automated acknowledgements

EMAIL
Subject: {subject}
From: {sender}
{body}
"""

KINDS = ("rejection", "recruiter", "interview", "offer", "noise")
MIN_SIGNALS = 2          # never classify on one weak coincidence

# Two different jobs, so two different lists. Company tokens must exclude
# industry words, or an employer called "Cloud Systems" matches every
# infrastructure posting in the inbox. Title tokens must NOT exclude them --
# "site reliability engineer" appearing in a subject line is exactly the
# evidence we are looking for, and stopwording it away is what made a real
# reply from careers@<company>.com score only one signal.
_COMPANY_STOP = {
    "the", "and", "for", "with", "india", "global", "group", "labs", "inc",
    "cloud", "systems", "technologies", "solutions", "services", "software",
    "digital", "data", "tech", "consulting", "networks",
}
_TITLE_STOP = {"the", "and", "for", "with", "your", "our", "job", "role",
               "application", "applying", "position", "opportunity"}
# ATS senders relay mail on the employer's behalf; their domain is a real signal
# that this is application mail, but it is never company-identifying on its own.
# The last six were read off real mail on 2026-10-27: Workable sends from
# workablemail.com (not workable.com), Oracle Recruiting Cloud from
# workflow.mail.<region>.cloud.oracle.com, Avature from avature.<employer>.net,
# SAP SuccessFactors from <employer>@noreply<n>.jobs2web.com, and PeopleStrong
# (an Indian HR suite) from <employer>'s name at peoplestrong.com.
ATS_DOMAINS = ("greenhouse.io", "greenhouse-mail.io", "lever.co", "ashbyhq.com",
               "workable.com", "myworkday.com", "smartrecruiters.com", "icims.com",
               "workablemail.com", "cloud.oracle.com", "avature.", "kula.ai",
               "jobs2web.com", "peoplestrong.com")
# Hosts an apply link can point at that are never the employer: a WWR row's
# apply_url is a Google search, its url the WWR posting.
NOT_EMPLOYER_DOMAINS = ("google.com", "weworkremotely.com")
# An ATS relay names the employer in its local part (<employer>@myworkday.com),
# which says as much as an employer's own domain. These local parts are shared
# by every employer and say nothing.
_SHARED_LOCAL = re.compile(
    r"reply|notif|^(?:careers?|jobs?|talent|recruit\w*|hr|info|hello|team|support)$")

# Job boards. Their alerts belong to gmail_alerts.py; from them, only a person
# writing through the board is a reply (see _portal_person). Nothing else from
# a board is matched against your applications: an alert reading "DevOps
# Engineer at <company>" names the company and the title, which is two signals,
# and on the first dry run (2026-10-27) alerts made up most of the matches.
# Domain -> the name a Telegram line uses. foundit is Monster India renamed and
# still mails from both; hackajob's "<employer> wants you to apply" is its own
# matching bot writing, not the employer.
PORTALS = {"linkedin.com": "LinkedIn", "naukri.com": "Naukri", "indeed.com": "Indeed",
           "glassdoor.com": "Glassdoor", "glassdoor.co.in": "Glassdoor",
           "foundit.in": "foundit", "monsterindia.com": "foundit", "shine.com": "Shine",
           "hirist.tech": "hirist", "hirist.com": "hirist", "cutshort.io": "Cutshort",
           "instahyre.com": "Instahyre", "adzuna.in": "Adzuna", "talent500.co": "Talent500",
           "wellfound.com": "Wellfound", "timesjobs.com": "TimesJobs",
           "internshala.com": "Internshala", "hackajob.com": "hackajob",
           "mail-hackajob.com": "hackajob"}
# A board's own mailers, as opposed to a recruiter's address on it.
_BULK_LOCAL = re.compile(
    r"^(?:info|team|support|hello|news|newsletter|updates?|notifications?)$"
    r"|alert|no-?reply|donotreply|do-not-reply")
# LinkedIn message notifications. Unlike the other rules here this one is not
# measured: the inbox it was written against had none in 60 days.
_LINKEDIN_MESSAGE = re.compile(
    r"\b(?:sent you a (?:new )?message|messaged you|new message|inmail)\b", re.I)
# A board's own mailer saying a company or recruiter acted on your profile.
# Measured 2026-10-27 over 60 days: this matched 35 messages, every one of them
# outreach -- 13 foundit "You're invited to apply for the <role> at <company>"
# ("we came across your profile"), 10 Cutshort "Missed message from <company>?"
# (an employer's message left unanswered), 8 Shine (recruiter contact,
# "You're on our Shortlist for <role>", a consultancy's interview call) and 4
# Naukri "recruiter(s) expressed interest in your Naukri profile". The boards'
# adverts did not match: "Recruiters are noticing your profile", "Top Recruiters
# Are Looking for You!", Cutshort's weekly "<companies> are hiring".
_PORTAL_OUTREACH = re.compile(
    r"\binvited to apply\b|\binvites? you to apply\b|\binvitation to apply\b"
    r"|\bmissed message from\b|\bsent you a (?:new )?message\b|\bmessaged you\b"
    r"|\bnew message from\b|\binmail\b"
    r"|\brecruiters? (?:contacted|messaged) you\b|\bexpressed interest in your\b"
    r"|\b(?:is|are) interested in your profile\b|\bon our shortlist\b|^interview\b"
    r"|\bverify (?:profile )?details\b", re.I)
# Shine relays a recruiter's message under the employer's name, "<Company> -
# Client of Shine.com", with only the role as the subject; the body opens "A
# recruiter has reached out to you". Measured 2026-10-28 over 60 days: 16 of
# these and 3 consultancy mails ending "Verify (Profile) Details" (a shortlist
# asking for an updated CV), all outreach, and neither pattern matched any
# other mail. Before this, 18 of the 19 were skipped.
_PORTAL_CLIENT = re.compile(r"^(.+?)\s+-\s+client of shine(?:\.com)?\b", re.I)

# Subjects of mail about an application. The model reads every match and
# calls an acknowledgement noise, so a wide net costs one call while a narrow
# one loses a status update in silence -- but "application" alone is too wide:
# it caught "OAuth application" security mail and "Applied Generative AI"
# course adverts on the first dry run. Every real application subject in 60
# days fits one of these. "thank you for your interest" usually opens a rejection.
_APPLICATION_SUBJECT = re.compile(
    r"\b(?:your|job) application\b|\bapplication (?:status|update|received|for|to)\b|"
    r"\bapplying\b|\bcandidature\b|\binterview|\bstatus update\b|\bnext steps?\b|"
    r"\bassessment\b|\bquestionnaire\b|\boffer letter\b|"
    r"\bthank(?:s| you) for your interest\b", re.I)
_BANKING = re.compile(
    r"\b(?:credit|debit) card\b|\bsavings\b|\ba/c\b|\bloan\b|\bkyc\b|\bnet ?banking\b", re.I)
# An applicant-tracking system also mails its employer's job alerts
# ("New job opportunities at <employer>", weekly from Oracle Recruiting Cloud).
_ATS_JOB_ALERT = re.compile(r"^new job opportunities\b|^new jobs posted\b|\bjob alert", re.I)

# Measured 2026-10-27 against 60 days of real mail: every recruiter who wrote
# to the Primary tab used at least one of these (in the subject or the body),
# and 10 of the 15 messages they matched were recruiters or employers.
_RECRUITER_WORDS = (
    '"notice period" OR CTC OR interview OR "your profile" OR "your resume" OR '
    '"your CV" OR "job opportunity" OR "job description" OR shortlisted OR '
    'recruiter OR "hiring for" OR "your application"')
_NOT_PRIMARY = "-category:updates -category:promotions -category:social -category:forums"

# Telegram refuses a message over 4096 characters; leave room for escaping.
TELEGRAM_LIMIT = 3500
_ORDER = {"offer": 0, "interview": 1, "recruiter": 2, "update": 3, "rejection": 4}
_LABEL = {"offer": "Offer", "interview": "Interview", "recruiter": "Recruiter",
          "update": "Application update", "rejection": "Rejection"}


def _lookback_days() -> int:
    """How far back each run reads. Each message is handled once whatever the
    overlap, so this only has to outlast the longest stretch the job might not
    run -- a week with the laptop shut, say."""
    return int(env("TRACK_LOOKBACK_DAYS", "14") or 14)


def _max_messages() -> int:
    return int(env("TRACK_MAX", "2000") or 2000)


def _queries(days: int) -> dict[str, str]:
    window = f"newer_than:{days}d"
    return {
        "all": f"{window} -from:me",
        # Newsletters and adverts: never matched against an application.
        "bulk": f"{window} (category:promotions OR category:social OR category:forums)",
        "recruiter": f"{window} -from:me {_NOT_PRIMARY} ({_RECRUITER_WORDS})",
        # The same words in any tab; only a named person's mail counts there.
        "words": f"{window} -from:me ({_RECRUITER_WORDS})",
    }


def _tokens(text: str, stop: set[str] = frozenset()) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{4,}", (text or "").lower())
            if w not in stop}


def _domain(url: str) -> str:
    host = urlparse(url or "").netloc.lower().removeprefix("www.")
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _is_ats(domain: str) -> bool:
    return any(d in domain for d in ATS_DOMAINS)


def _is_portal(domain: str) -> bool:
    return any(domain == p or domain.endswith("." + p) for p in PORTALS)


def _signals(job, sender: str, subject: str) -> tuple[int, list[str]]:
    """Independent reasons to believe this email is about this application."""
    sender, subject = sender.lower(), subject.lower()
    sender_domain = sender.split("@")[-1].strip("> ")
    local = parseaddr(sender)[1].partition("@")[0]
    company_tokens = _tokens(canon_company(job["company"]), _COMPANY_STOP)
    found = []

    # The employer's own mail server. The strongest signal there is.
    if any(tok in sender_domain for tok in company_tokens):
        found.append(f"sender-domain:{sender_domain}")

    # The apply link's own domain, when it is the employer's and not the ATS's.
    dom = _domain(job["apply_url"] or job["url"])
    if (dom and not any(a in dom for a in ATS_DOMAINS + NOT_EMPLOYER_DOMAINS)
            and dom in sender):
        found.append(f"apply-domain:{dom}")

    for tok in company_tokens:
        if re.search(rf"(?<![a-z-]){re.escape(tok)}(?![a-z])", subject):
            found.append(f"subject-company:{tok}")
            break

    title_hits = _tokens(job["title"], _TITLE_STOP) & _tokens(subject, _TITLE_STOP)
    if len(title_hits) >= 2:
        found.append(f"title:{'+'.join(sorted(title_hits)[:2])}")

    if _is_ats(sender_domain):
        found.append("ats-sender")
        # A Workday "Your Application" with no company in the subject scored
        # one signal here and was never recorded (2026-10-27).
        if (local and not _SHARED_LOCAL.search(local)
                and any(tok in local for tok in company_tokens)):
            found.append(f"ats-local:{local}")

    return len(found), found


def _index(applied) -> dict[str, list]:
    """Applications by company token, built once rather than rescanning every
    application per message. Same-company applications share a key and are
    separated afterwards by signal strength."""
    by_token: dict[str, list] = defaultdict(list)
    for job in applied:
        for tok in _tokens(canon_company(job["company"]), _COMPANY_STOP):
            by_token[tok].append(job)
    return by_token


def _match(sender: str, subject: str, by_token) -> tuple[bool, object, list[str]]:
    """(matched, the application or None, why). Matched with no application
    means two applications fit equally well: genuinely ambiguous, so the mail
    is still reported but neither application is closed on a guess."""
    hay_tokens = _tokens(f"{sender} {subject}", _COMPANY_STOP)
    candidates = {j["id"]: j for tok in hay_tokens for j in by_token.get(tok, [])}
    # An ATS local part runs the employer's words together ("acmegroup@"),
    # so whole-token lookup misses it; _signals' ats-local check would not.
    local, _, domain = parseaddr(sender)[1].lower().partition("@")
    if _is_ats(domain) and local and not _SHARED_LOCAL.search(local):
        for tok, jobs in by_token.items():
            if tok in local:
                candidates.update({j["id"]: j for j in jobs})
    ranked = []
    for job in candidates.values():
        n, why = _signals(job, sender, subject)
        if n >= MIN_SIGNALS:
            ranked.append((n, job, why))
    if not ranked:
        return False, None, []
    ranked.sort(key=lambda x: -x[0])
    tied = [r for r in ranked if r[0] == ranked[0][0]]
    if len(tied) > 1:
        return True, None, [f"ambiguous: {', '.join(f'#{t[1]['id']}' for t in tied)}"]
    return True, ranked[0][1], ranked[0][2]


def _portal_person(addr: str, subject: str) -> str | None:
    """The board's name when a person wrote through it, else None. Read off
    real mail (2026-10-27), except LinkedIn's (see _LINKEDIN_MESSAGE)."""
    local, _, domain = addr.partition("@")
    if domain == "naukri.com" or domain.endswith(".naukri.com"):
        # A recruiter's Naukri mail comes from their name plus a token
        # ("priya.k0dgvz...=@naukri.com"); alerts from info@, naukrialerts@.
        return None if _BULK_LOCAL.search(local) else "naukri"
    if domain.endswith("reply.cutshort.io"):
        return "cutshort"     # name+token@reply.cutshort.io: a recruiter's reply
    if (domain == "linkedin.com" or domain.endswith(".linkedin.com")) \
            and _LINKEDIN_MESSAGE.search(subject):
        return "linkedin"
    return None


def _board(addr: str) -> str:
    domain = addr.lower().partition("@")[2]
    return next((name for p, name in PORTALS.items()
                 if domain == p or domain.endswith("." + p)), domain)


def _portal_invite(addr: str, subject: str, name: str = "") -> str | None:
    """The board's name when its own mail says a company reached out (see
    _PORTAL_OUTREACH and _PORTAL_CLIENT), else None."""
    if _PORTAL_OUTREACH.search(html.unescape(subject or "")) or _PORTAL_CLIENT.search(name):
        return _board(addr)
    return None


def _person_sender(sender: str) -> bool:
    """A named person's own mailbox rather than a company's mailer: a display
    name of two or more words, one of them in the address's local part and
    none in its domain. "Jane Doe <jane.doe@agency.example>" is a person;
    "Dbco <dbco@team.dbco.example>" (one word), "Acme Bank <acme_bank@
    acme.example>" (named in its domain) and "Sam from Jobtool
    <hello@jobtool.example>" (a mailer's address) are not.
    Measured 2026-10-27 outside the Primary tab over 60 days: with the
    recruiter words, this kept a staffing firm's recruiter whose mail Gmail had
    filed in Promotions, and nothing else."""
    name, addr = parseaddr(sender)
    local, _, domain = addr.lower().partition("@")
    words = re.findall(r"[a-z]+", name.lower())
    if len(words) < 2 or not local or _BULK_LOCAL.search(local):
        return False
    named = [w for w in words if len(w) >= 3]
    return any(w in local for w in named) and not any(w in domain for w in named)


def _sort(h: dict, bulk: set[str], primary_recruiter: set[str], by_token,
          words: set[str] = frozenset()):
    """(bucket, board name, application, why) for a message worth a look, else None.

    `bulk` holds the ids in the Promotions, Social and Forums tabs,
    `primary_recruiter` the Primary-tab ids that used recruiter words, and
    `words` the ids in any tab that used them; only the server knows a
    message's tab, so all three come from their own searches."""
    hd = h["headers"]
    sender, subject = hd.get("from", ""), hd.get("subject", "")
    addr = parseaddr(sender)[1].lower()
    domain = addr.partition("@")[2]

    if _is_portal(domain):
        portal = _portal_person(addr, subject)
        if portal:
            return "portal", portal, None, []
        board = _portal_invite(addr, subject, parseaddr(sender)[0])
        return ("invite", board, None, []) if board else None
    # Banks write about "your application" too -- for an account, a card, a
    # loan. Judged by the subject, not the sender: a bank is also an employer.
    if _BANKING.search(subject):
        return None
    if _is_ats(domain):
        if _ATS_JOB_ALERT.search(subject):
            return None
        _, job, why = _match(sender, subject, by_token)
        return "application", None, job, why
    named = h["id"] in words and _person_sender(sender)
    if h["id"] in bulk:
        return ("recruiter", None, None, []) if named else None
    matched, job, why = _match(sender, subject, by_token)
    if matched or _APPLICATION_SUBJECT.search(subject):
        return "application", None, job, why
    if h["id"] in primary_recruiter or named:
        return "recruiter", None, None, []
    return None


def _unfold(h: dict) -> dict:
    """Long headers arrive folded over several lines ("Some Company\\r\\n
    <recruiter@...>"), which would put line breaks into the log and the
    Telegram message. Folding whitespace means one space."""
    return {**h, "headers": {k: " ".join(str(v).split()) for k, v in h["headers"].items()}}


def _message_id(h: dict) -> str:
    mid = (h["headers"].get("message-id") or "").strip().strip("<>")
    if mid:
        return mid
    raw = "|".join(h["headers"].get(k, "") for k in ("from", "subject", "date"))
    return "sha1:" + hashlib.sha1(raw.encode()).hexdigest()


def _received(hd: dict) -> str | None:
    try:
        when = parsedate_to_datetime(hd.get("date", ""))
    except (TypeError, ValueError):
        return None
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


def _classify(sender: str, subject: str, msg) -> str:
    """The model's kind for application mail, validated. Anything it cannot
    answer cleanly is an "update": the user is told and opens the mail, which
    beats losing a reply to a 503 or a malformed answer."""
    from .gmail import body_text
    try:
        res = generate_json(
            PROMPT.format(subject=subject, sender=sender, body=body_text(msg)[:3000]),
            model=MODEL_SCORE, temperature=0.0)
    except Exception:
        return "update"
    kind = res.get("kind") if isinstance(res, dict) else None
    return kind if kind in KINDS else "update"


# ---------------------------------------------------------------------------
# the Telegram message
# ---------------------------------------------------------------------------
def _who(row) -> str:
    if row["job_company"]:
        return row["job_company"]
    name, addr = parseaddr(row["sender"] or "")
    return name or addr.partition("@")[2] or addr or "unknown sender"


def _when(row) -> str:
    try:
        return datetime.fromisoformat(row["received_at"]).strftime("%d %b").lstrip("0")
    except (TypeError, ValueError):
        return ""


def _naukri_title(subject: str) -> str:
    """A Naukri recruiter mail's subject without its "✉️ Job | " prefix."""
    s = re.sub(r"^[^\w]+", "", subject or "")
    return re.sub(r"^job\s*\|\s*", "", s, flags=re.I)[:70]


_INVITE_PREFIX = re.compile(
    r"^(?:(?:re|fwd?)\s*:\s*|congratulations!?\s*|you(?:'|’)re invited to apply for (?:the )?)",
    re.I)


def _invite_title(subject: str, sender: str = "") -> str:
    """"You're invited to apply for the SRE role at Acme" -> "SRE role at Acme".
    A Shine client mail's subject is only the role, so its company is added
    from the sender's name."""
    s, prev = html.unescape(subject or "").strip(), None
    while s != prev:
        prev, s = s, _INVITE_PREFIX.sub("", s).strip()
    client = _PORTAL_CLIENT.search(parseaddr(sender or "")[0])
    if client:
        s = f"{s[:60]} ({client.group(1).strip()[:40]})"
    return s[:100] if client else s[:80]


def _block(row) -> str:
    meta = [parseaddr(row["sender"] or "")[1], _when(row)]
    if row["job_id"]:
        meta.append(f"your application #{row['job_id']}: {row['job_title'] or ''}".strip())
    return "\n".join([
        f"*{_LABEL.get(row['kind'], 'Reply')}* - {notify._md(_who(row))}",
        notify._md((row["subject"] or "(no subject)")[:200]),
        notify._md(" · ".join(m for m in meta if m)),
    ])


def messages(rows) -> list[str]:
    """The alert as Telegram messages, most important first, each under the
    size limit. A recruiter's Naukri mail is usually a job sent to many people
    at once, so those share one line of titles instead of a block each."""
    rows = list(rows)
    naukri = [r for r in rows if r["kind"] == "naukri"]
    invites: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        if r["kind"] == "invite":
            title = _invite_title(r["subject"], r["sender"])
            board = invites[_board(parseaddr(r["sender"] or "")[1])]
            if title not in board:          # Cutshort repeats its reminders
                board.append(title)
    rest = sorted((r for r in rows if r["kind"] not in ("naukri", "invite")),
                  key=lambda r: _ORDER.get(r["kind"], 9))
    n = len(rows)
    blocks = [f"*📬 {n} new from recruiters and employers*"]
    blocks += [_block(r) for r in rest]
    if naukri:
        titles = "; ".join(_naukri_title(r["subject"]) for r in naukri[:8])
        more = f" (+{len(naukri) - 8} more)" if len(naukri) > 8 else ""
        blocks.append(f"*Naukri recruiter mails ({len(naukri)}):* "
                      f"{notify._md(titles)}{more}")
    # A board's notice that a company reached out: the company and role are
    # the subject, so one line per board says it all.
    for board, titles in sorted(invites.items(), key=lambda kv: -len(kv[1])):
        more = f" (+{len(titles) - 8} more)" if len(titles) > 8 else ""
        blocks.append(f"*{notify._md(board)} - companies reaching out ({len(titles)}):* "
                      f"{notify._md('; '.join(titles[:8]))}{more}")
    out, cur = [], ""
    for b in blocks:
        if cur and len(cur) + 2 + len(b) > TELEGRAM_LIMIT:
            out.append(cur)
            cur = b
        else:
            cur = f"{cur}\n\n{b}" if cur else b
    if cur:
        out.append(cur)
    return out


def _send(rows, log) -> bool:
    """Send what is new and mark it sent. False when Telegram refused; the rows
    stay unsent and the next run sends them again."""
    if not rows:
        return True
    texts = messages(rows)
    if not (env("TELEGRAM_BOT_TOKEN") and env("TELEGRAM_CHAT_ID")):
        # No bot configured: the log is the only place this can go.
        for t in texts:
            log(t)
        db.mark_replies_sent([r["message_id"] for r in rows])
        return True
    for t in texts:
        if not notify._send(t):
            return False
    db.mark_replies_sent([r["message_id"] for r in rows])
    return True


def _gmail_failed(e: Exception, days: int, log) -> NoReturn:
    """A check that could not run must not look like a quiet inbox -- this
    project's most repeated bug (CLAUDE.md 7, "produced nothing, reported
    success"). Say so on Telegram, record it, and fail the stage."""
    why = f"{type(e).__name__}: {e}"[:600]
    log(f"gmail unavailable: {why}")
    db.log_run("track", False, f"gmail unavailable: {why}")
    notify._send(
        "*JobPipe could not read Gmail to look for replies.*\n"
        f"{notify._md(why)}\n"
        f"The next run looks back {days} days, so nothing is lost if it works then.")
    raise RuntimeError(f"gmail unavailable: {why}") from e


def run(log=print, *, days: int | None = None, dry_run: bool = False) -> None:
    from . import gmail
    days = days or _lookback_days()
    cap = _max_messages()
    if not gmail.configured():
        log("gmail not configured -- skipping reply tracking")
        return

    q = _queries(days)
    # One Gmail login for the whole run (gmail.session). The try blocks stay
    # around the Gmail calls alone, so a bug elsewhere is never reported as
    # "could not read Gmail".
    with ExitStack() as stack:
        try:
            g = stack.enter_context(gmail.session())
            heads = [_unfold(h) for h in g.search_headers(q["all"], max_results=cap)]
            bulk = g.search_ids(q["bulk"], max_results=cap)
            recruiter = g.search_ids(q["recruiter"], max_results=cap)
            words = g.search_ids(q["words"], max_results=cap)
        except Exception as e:
            _gmail_failed(e, days, log)
        log(f"read {len(heads)} message headers from the last {days} days"
            + (" -- AT THE CAP, raise TRACK_MAX" if len(heads) >= cap else ""))

        by_token = _index(db.fetch(status="applied", limit=500))
        seen = db.seen_replies([_message_id(h) for h in heads])
        picked = []
        for h in heads:
            mid = _message_id(h)
            if mid in seen:
                continue
            found = _sort(h, bulk, recruiter, by_token, words)
            if found:
                picked.append((mid, h, *found))

        need = [] if dry_run else [h["id"] for _, h, bucket, *_ in picked
                                   if bucket == "application" and "msg" not in h]
        try:
            bodies = g.fetch(need) if need else {}
        except Exception as e:
            _gmail_failed(e, days, log)

    if dry_run:
        log(f"{len(picked)} new message(s) would be looked at (no model call, "
            f"nothing sent or written):")
        for _, h, bucket, portal, job, why in picked:
            hd = h["headers"]
            what = portal or bucket
            app = f"  -> #{job['id']} {job['company']}" if job else ""
            log(f"  {(_received(hd) or '')[:10]}  {what:11s} "
                f"{parseaddr(hd.get('from', ''))[1][:40]:40s} {hd.get('subject', '')[:60]}"
                f"{app}{'  ' + ', '.join(why) if why and not job else ''}")
        return

    counts: Counter = Counter()
    for mid, h, bucket, portal, job, why in picked:
        hd = h["headers"]
        sender, subject = hd.get("from", ""), hd.get("subject", "")
        if bucket == "application":
            msg = h.get("msg") or bodies.get(h["id"])
            kind = _classify(sender, subject, msg) if msg is not None else "update"
        elif bucket == "invite":
            kind = "invite"
        else:
            kind = "naukri" if portal == "naukri" else "recruiter"
        db.add_reply(mid, sender, subject, kind, received_at=_received(hd),
                     job_id=job["id"] if job is not None and kind != "noise" else None)
        if job is not None and kind != "noise":
            db.update(job["id"], status="responded", response_at=now(),
                      response_kind=kind)
        counts[kind] += 1
        if kind != "noise":
            log(f"  {kind}: {sender[:50]} - {subject[:60]}"
                + (f" [#{job['id']}: {', '.join(why)}]" if job is not None
                   else f" [{', '.join(why)}]" if why else ""))

    unsent = db.unsent_replies()
    delivered = _send(unsent, log)

    # Follow-up nudges
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    stale = [r for r in db.fetch(status="applied", limit=200)
             if (r["applied_at"] or "") < cutoff and not r["response_at"]]
    for r in stale:
        db.update(r["id"], followup_due=now())

    new = sum(n for k, n in counts.items() if k != "noise")
    summary = (f"{new} new repl{'y' if new == 1 else 'ies'}"
               f" ({', '.join(f'{n} {k}' for k, n in sorted(counts.items()))})" if counts else
               "0 new replies")
    if unsent:
        summary += (f" | {len(unsent)} sent to telegram" if delivered
                    else f" | telegram refused {len(unsent)}, kept for the next run")
    log(f"{summary} | {len(stale)} due a follow-up nudge")
    db.log_run("track", delivered, summary)
    if not delivered:
        raise RuntimeError("telegram refused the reply alert; it is kept for the next run")
