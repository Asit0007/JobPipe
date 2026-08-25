"""Two-stage scoring: free deterministic prefilter, then Gemini on survivors.

The prefilter exists to protect the 1500/day free-tier quota. Roughly 70% of
ingested postings die here for free, so the LLM budget goes to the ones that
could plausibly be a fit.
"""
from __future__ import annotations

import json

from . import db
from .config import MODEL_SCORE, profile
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


def prefilter(job) -> tuple[bool, int, str]:
    p = profile()
    blob = f"{job['title']} {job['description'] or ''}".lower()

    for term in p.get("hard_reject", []):
        if term.lower() in blob:
            return False, 0, f"hard reject: {term}"

    hits = sum(1 for kw in p["must_have_any"] if kw.lower() in blob)
    if hits < p["thresholds"]["keyword_prefilter_min"]:
        return False, hits, f"only {hits} must-have keyword(s)"
    return True, hits, "passed"


def apply_penalties(score: int, job) -> tuple[int, list[str]]:
    p = profile()
    blob = f"{job['title']} {job['description'] or ''}".lower()
    applied = []
    for term, penalty in (p.get("soft_penalty") or {}).items():
        if term.lower() in blob:
            score -= int(penalty)
            applied.append(f"-{penalty} ({term})")
    if looks_like_staffing_firm(job["company"], job["description"] or ""):
        score -= 8
        applied.append("-8 (staffing repost)")
    return max(0, min(100, score)), applied


def run(log=print) -> None:
    p = profile()
    facts_cfg = __import__("jobpipe.config", fromlist=["facts"]).facts()
    skills = facts_cfg.get("skills", {})
    rows = db.fetch(status="discovered", limit=1000)
    log(f"scoring {len(rows)} jobs | Gemini budget left: {budget_remaining()}")

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
