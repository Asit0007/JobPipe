"""Screening-question answers -- the one genuinely great idea in AIHawk,
rebuilt without the browser automation or the stored password.

These are the questions that actually eat your evening: every portal asks the
same twelve, worded differently. We generate your answers once, keep the
sensitive ones under human control, and hand you a copy-paste panel.
"""
from __future__ import annotations

import json

from .config import MODEL_TAILOR, facts, profile
from .llm import generate_json

# Questions the pipeline will NEVER answer on your behalf. These are
# negotiating positions, not data-entry fields.
HUMAN_ONLY = {
    "notice_period": "Your call, per employer. Check profile.yaml notice_period_days.",
    "current_ctc": "Deflect unless legally required. profile.yaml has disclose_current_ctc: false.",
    "expected_ctc": "Anchor high. Never the first number if you can avoid it.",
    "willing_to_relocate": "Depends on the offer. Do not pre-commit in a form.",
    "reason_for_leaving": "Keep it forward-looking and short. Never negative about your current employer.",
}

PROMPT = """Draft answers to standard job-application screening questions.

CANDIDATE FACTS (the only truth available -- do not exceed them):
Strong skills: {strong}
Working knowledge: {working}
Some exposure: {exposure}
Years of experience: {years}
Verified accomplishments:
{bullets}

TARGET JOB
{title} at {company}
{description}

Return JSON:
{{
  "answers": [
    {{"question":"<the standard question>","answer":"<answer, max 40 words>","confidence":"high"|"medium"|"low"}}
  ]
}}

Cover these questions:
1. Years of experience with the primary technology in this JD
2. Years of experience with the secondary technology in this JD
3. Why are you interested in this role
4. Describe a production incident you handled
5. What is your experience with automation and infrastructure as code
6. Are you authorised to work in India
7. Do you have experience in a 24x7 or on-call environment

RULES:
- If the JD demands a technology the candidate only has exposure to, answer with the
  true lower number and mark confidence "low". Never inflate a year count.
- No filler, no enthusiasm padding. Plain declarative sentences.
- Answer 4 using ONLY the verified accomplishments listed above.
"""


def generate_for(job, model: str | None = None) -> dict:
    cfg, p = facts(), profile()
    skills = cfg.get("skills", {})
    bullets = [
        f["text"]
        for group in (cfg.get("roles", []) + cfg.get("projects", []))
        for f in (group.get("facts") or [])
        if f.get("verified")
    ]
    if not bullets:
        return {"answers": [], "error": "No verified facts in config/facts.yaml"}

    out = generate_json(
        PROMPT.format(
            strong=", ".join(skills.get("strong", [])),
            working=", ".join(skills.get("working", [])),
            exposure=", ".join(skills.get("exposure", [])),
            years=p["identity"]["years_experience"],
            bullets="\n".join(f"- {b}" for b in bullets),
            title=job["title"], company=job["company"],
            description=(job["description"] or "")[:4000],
        ),
        model=model or MODEL_TAILOR, temperature=0.3,
    )
    out["human_only"] = HUMAN_ONLY
    return out


def render(payload: dict) -> str:
    lines = ["## Screening answers\n"]
    for a in payload.get("answers", []):
        flag = {"high": "", "medium": " *(verify)*", "low": " **(weak -- consider skipping this role)**"}
        lines.append(f"**{a['question']}**{flag.get(a.get('confidence'), '')}  \n{a['answer']}\n")
    lines.append("\n## You answer these yourself\n")
    for k, v in payload.get("human_only", {}).items():
        lines.append(f"- **{k.replace('_',' ').title()}** - {v}")
    return "\n".join(lines)
