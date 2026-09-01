"""Two-stage scoring: free deterministic prefilter, then Gemini on survivors.

The prefilter exists to protect the free-tier quota. Roughly 70% of ingested
postings die here for free, so the LLM budget goes to the ones that could
plausibly be a fit.
"""
from __future__ import annotations

import json
import re

from . import db, jdfetch
from .config import MODEL_SCORE, facts, profile
from .llm import QuotaExhausted, budget_remaining, generate_json
from .normalize import looks_like_staffing_firm

PROMPT = """You are screening a job posting for an infrastructure engineer with
{years} years of experience. Be strict and realistic. Most postings are a poor fit;
say so rather than being generous.

CANDIDATE SKILL PROFILE
Strong: {strong}
Working knowledge: {working}
Some exposure: {exposure}
Target titles: {titles}
Acceptable locations: {locations}

JOB POSTING
Title: {title}
Company: {company}
Location: {location}
Description:
{description}

Return JSON exactly:
{{
  "score": <0-100 integer, honest fit>,
  "reason": "<max 25 words, the single most decisive factor>",
  "seniority_match": "under" | "good" | "over",
  "missing_skills": ["<skills the JD requires that the candidate lacks>"],
  "red_flags": ["<vague JD, unrealistic stack, shift work, staffing repost, etc>"],
  "tailor_notes": "<max 30 words: which of the candidate's areas to lead with>"
}}

Scoring guidance:
- 85+ : strong match, candidate meets nearly all stated requirements
- 70-84: good match with 1-2 gaps that are learnable
- 50-69: stretch, real gaps
- <50  : not a fit
Penalise heavily if the required experience exceeds {years} years by more than 3.
"""

# --------------------------------------------------------------------------
# Hard reject, with the false-negative fixed
#
# A plain substring test on "10+ years" also kills "our team has 10+ years of
# combined experience" -- a perfectly good posting, silently discarded. Terms
# that look like an experience requirement now have to appear in one, and not
# inside a sentence that is describing the team rather than the candidate.
# --------------------------------------------------------------------------
_YEARS_TERM = re.compile(r"\d+\s*\+?\s*(years?|yrs?)", re.I)
_WINDOW = 60
_EXPERIENCE = re.compile(r"\bexp(erience|\.)?\b", re.I)
_NOT_ABOUT_YOU = re.compile(
    r"\b(combined|collective|cumulative|between us|our team|the team has|we have|"
    r"founded|in business|serving clients|track record)\b", re.I
)


def _hard_reject_hit(term: str, blob: str) -> bool:
    """True if `term` is a genuine disqualifier in this text."""
    if not _YEARS_TERM.search(term):
        return term in blob                      # plain terms stay plain

    for m in re.finditer(re.escape(term), blob):
        lo = max(0, m.start() - _WINDOW)
        window = blob[lo:m.end() + _WINDOW]
        if not _EXPERIENCE.search(window):
            continue                             # a number, but not about experience
        if _NOT_ABOUT_YOU.search(window):
            continue                             # describing the company, not the role
        return True
    return False


def _is_alert_without_jd(job) -> bool:
    """An email-alert row that has no usable job description.

    Both halves still matter. `alert:` alone is not enough -- once jdfetch has
    fetched the posting page the row has a real JD and should face the normal
    keyword gate like anything else.

    But "has a description" is NOT the same as "is non-empty", and reading it
    that way cost 86 jobs on 2026-08-29. Bug 7.24 taught the extractor to keep
    the email's blurb, so alert rows stopped being blank and started carrying a
    ~120-character fragment -- "Mentor junior DevOps engineers and conduct
    technical design reviews." That is not a JD. It is too short to hold two
    must-have keywords, so the row failed the gate it had just become eligible
    for. Measured on the live corpus: of 105 alert rows holding a fragment,
    **86 died on the keyword count**, and they were titles like "Senior DevOps
    Engineer" and "Lead Cloud Engineer - DevOps / SecOps". Meanwhile all 26
    genuinely blank rows sailed through the carve-out and 12 of them
    shortlisted. **7.25 returning through the door 7.24 opened.**

    So the threshold is `jdfetch.MIN_USEFUL_CHARS`, the number jdfetch already uses to
    decide a description is not worth having. One definition of "usable", used
    by both modules.
    """
    try:
        source = job["source"] or ""
    except (KeyError, IndexError):
        return False
    if not source.startswith("alert:"):
        return False
    return len((job["description"] or "").strip()) < jdfetch.MIN_USEFUL_CHARS


def prefilter(job) -> tuple[bool, int, str]:
    p = profile()
    title = (job["title"] or "").lower()
    blob = f"{job['title']} {job['description'] or ''}".lower()

    # Title-level reject, checked first because it is the cheapest and the most
    # decisive. must_have_any matches title+description, so an "Enterprise
    # Account Executive" post whose JD lists the AWS and Kubernetes products it
    # sells sails straight through into an LLM call. Measured against a real
    # 4,654-posting ingest, GTM roles were 44% of everything that survived.
    for term in p.get("title_reject", []) or []:
        if term.lower() in title:
            return False, 0, f"title reject: {term}"

    for term in p.get("hard_reject", []):
        if _hard_reject_hit(term.lower(), blob):
            return False, 0, f"hard reject: {term}"

    hits = sum(1 for kw in p["must_have_any"] if kw.lower() in blob)

    # A job-alert row that arrived with no description has only its title to
    # count keywords in, and a title almost never carries two of them. Measured
    # on the 4,654-row corpus with descriptions stripped: "DevOps Engineer"
    # scores 0 hits and "AWS Cloud Engineer" scores 1 -- both killed, silently,
    # for free. That is bug 7.2 coming back through a different door, and it
    # would have thrown away the entire LinkedIn/Indeed/Naukri channel.
    #
    # The keyword count exists to protect the LLM budget from thousands of ATS
    # rows. It is the wrong instrument here: an alert row is already role-
    # filtered by the user on the platform, and alerts arrive at tens per day,
    # not thousands. title_reject and hard_reject still apply -- those are the
    # filters that keep genuinely wrong roles out.
    #
    # A title-match against targets.titles was measured as the alternative and
    # REJECTED: it kept only 3-5 of the 17 rows that scored >= 45, because real
    # titles ("Staff Engineer DevOps, Data Security (DLP)") do not look like
    # anything on a hand-written list. Failing open costs one LLM call; failing
    # closed loses the job silently, and silence is the expensive direction.
    if _is_alert_without_jd(job):
        return True, hits, f"alert row, no JD -- keyword gate skipped ({hits} hit(s))"

    if hits < p["thresholds"]["keyword_prefilter_min"]:
        return False, hits, f"only {hits} must-have keyword(s)"
    return True, hits, "passed"


# Working conditions are stated in the body and never in the title. Every other
# soft_penalty term names the ROLE, so it belongs to the title alone.
BODY_MATCHED_PENALTIES = ("night shift", "rotational shift")

# Remote is a stated preference, and it is also the only thing that makes the
# Bhubaneswar and Uttarakhand targets viable at the 9-14 LPA band -- the
# Dehradun market advertises roughly 3-5 LPA, below the floor.
#
# Matched on the LOCATION field alone, which is bug 7.18 applied before it can
# happen again: "remote" is ordinary prose in a description ("remote hands",
# "supporting remote teams", "remote desktop") and appears in JDs for roles
# that are fully onsite. A location field saying Remote is unambiguous.
#
# +5, not +1: the scorer's output clusters on multiples of 5 (45, 70, 75, 80,
# 83, 85) against a threshold of 45, so +1 would never move a row across it.
# The magnitude matches the existing -5 for a truncated Adzuna JD.
REMOTE_BONUS = 5
REMOTE_TERMS = ("remote", "work from home", "wfh")


def _column(job, key: str) -> str:
    """One column, lowercased, from either a dict or a sqlite3.Row.

    sqlite3.Row has no .get() and raises IndexError for an absent key, while a
    dict raises KeyError. Same shape as claims._field(), and it exists for the
    same reason: production passes a Row, the tests pass a dict.
    """
    try:
        return (job[key] or "").lower()
    except (KeyError, IndexError):
        return ""


def _is_remote(job) -> bool:
    """The schema's own `remote` flag, with a location-text fallback.

    `base.make_job()` already sets `remote` at ingest, from location AND title.
    Re-deriving the same idea here from the location alone was the defect: two
    definitions of one concept, disagreeing. Measured 2026-08-31 over 7,240
    rows, they differed on 29 -- and the ones worth having were Adzuna's
    "Senior AWS Cloud Engineer (Remote, Full-Time)" and "SRE Engineer, Remote",
    both location=India. Title says remote, location does not. That is the
    India-located remote inventory this search exists to find.

    Reading the title does admit a known false positive -- "2nd Level & Remote
    Support", where remote describes the work rather than the arrangement. It
    is accepted: this is a +5 nudge, not a gate, and title_reject/hard_reject
    already kill the roles it lands on. One definition, shared, beats two that
    disagree -- the lesson 7.2 -> 7.25 -> 7.38 taught three times.
    """
    try:
        flag = job["remote"]
    except (KeyError, IndexError):
        flag = None
    if flag is not None:
        return bool(flag)
    return any(t in _column(job, "location") for t in REMOTE_TERMS)


def apply_penalties(score: int, job) -> tuple[int, list[str]]:
    p = profile()
    title = (job["title"] or "").lower()
    body = f"{title} {(job['description'] or '').lower()}"
    applied = []
    # Matched on the TITLE. "salesforce" appears in 520 of 4,654 descriptions --
    # investor lists ("Salesforce Ventures"), integration catalogues, CRM tooling
    # -- and in 7 titles. Against the description its -40 sank good roles on the
    # strength of a funding paragraph. Same trap as title_reject: only the title
    # carries the signal.
    for term, penalty in (p.get("soft_penalty") or {}).items():
        t = term.lower()
        if t in (body if t in BODY_MATCHED_PENALTIES else title):
            score -= int(penalty)
            applied.append(f"-{penalty} ({term})")
    if looks_like_staffing_firm(job["company"], job["description"] or "",
                                job["source"]):
        score -= 8
        applied.append("-8 (staffing repost)")
    # Adzuna returns snippets, not full JDs, so its scores are guesses made on
    # a third of the evidence. Nudge them down rather than trusting them.
    if job["source"] == "adzuna" and len(job["description"] or "") < 800:
        score -= 5
        applied.append("-5 (truncated JD)")
    if _is_remote(job):
        score += REMOTE_BONUS
        applied.append(f"+{REMOTE_BONUS} (remote)")
    return max(0, min(100, score)), applied


def run(log=print) -> None:
    p = profile()
    skills = facts().get("skills", {})

    stale_days = p["thresholds"].get("stale_after_days")
    if stale_days:
        n = db.archive_stale(int(stale_days))
        if n:
            log(f"archived {n} posting(s) not seen in {stale_days} days")

    # Postings that arrived without a description would die in the prefilter for
    # free and for the wrong reason. Fill them in first.
    jdfetch.run(log=log)

    rows = db.fetch(status="discovered", limit=1000)
    log(f"scoring {len(rows)} jobs | {MODEL_SCORE} budget left: "
        f"{budget_remaining(MODEL_SCORE)}")

    scored = killed = 0
    for job in rows:
        ok, hits, why = prefilter(job)
        if not ok:
            db.update(job["id"], status="filtered", prefilter_hits=hits, score_reason=why)
            killed += 1
            continue

        try:
            result = generate_json(
                PROMPT.format(
                    years=p["identity"]["years_experience"],
                    strong=", ".join(skills.get("strong", [])),
                    working=", ".join(skills.get("working", [])),
                    exposure=", ".join(skills.get("exposure", [])),
                    titles=", ".join(p["targets"]["titles"]),
                    locations=", ".join(p["identity"]["locations_ok"]),
                    title=job["title"], company=job["company"],
                    location=job["location"] or "unspecified",
                    description=(job["description"] or "")[:6000],
                ),
                model=MODEL_SCORE,
            )
        except QuotaExhausted as e:
            log(f"! {e}\n  Stopping cleanly. Remaining jobs stay 'discovered' for tomorrow.")
            break
        except Exception as e:
            log(f"  score failed for {job['title'][:40]}: {type(e).__name__}")
            continue

        raw = int(result.get("score", 0))
        final, penalties = apply_penalties(raw, job)
        reason = result.get("reason", "")
        if penalties:
            reason += f" [{', '.join(penalties)}]"

        status = ("shortlisted" if final >= p["thresholds"]["shortlist_min_score"]
                  else "scored")
        db.update(
            job["id"], status=status, score=final, prefilter_hits=hits,
            score_reason=reason,
            missing_skills=json.dumps(result.get("missing_skills", [])),
            red_flags=json.dumps(result.get("red_flags", [])),
            tailor_notes=result.get("tailor_notes", ""),
        )
        scored += 1

    log(f"done: {scored} scored, {killed} filtered for free (no LLM call spent)")
    db.log_run("score", True, f"{scored} scored / {killed} prefiltered")
