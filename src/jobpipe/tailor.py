"""Resume tailoring, bounded by the facts file.

The model is handed a menu of fact IDs and must return a SELECTION. Every ID it
returns is validated against facts.yaml. Anything unverified or unrecognised is
dropped and reported. The model cannot add experience you do not have, because
it never gets to write the experience -- only choose among yours.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import db, screening
from .config import MODEL_TAILOR, OUT_DIR, facts
from .llm import QuotaExhausted, generate_json

PROMPT = """Select and rephrase resume bullets for a specific job application.

AVAILABLE FACTS (you may ONLY use these IDs -- there are no others):
{menu}

JOB
Title: {title}
Company: {company}
Description:
{description}

Emphasis hint from screening: {tailor_notes}

Return JSON exactly:
{{
  "selected": [
    {{"id": "<fact id from the menu>", "rewritten": "<same fact, reworded to echo the JD's language>"}}
  ],
  "summary": "<2 sentence professional summary, built only from selected facts>",
  "cover_note": "<4 sentences max, specific to this company, no flattery, no cliches>",
  "gap_honesty": "<1 sentence: the biggest genuine gap and how you would frame it if asked>"
}}

HARD RULES:
- Use 6 to 9 fact IDs. Never output an ID that is not in the menu above.
- "rewritten" must preserve the factual claim exactly. Change wording, never substance.
- Do not add metrics, percentages, team sizes, or durations that are not in the fact text.
- If the job is a poor match for the available facts, say so in gap_honesty rather
  than stretching the facts to fit.
"""


def _menu() -> tuple[str, dict]:
    """Build the allowed-fact menu. Unverified facts are excluded entirely."""
    cfg = facts()
    allowed: dict[str, dict] = {}
    lines, blocked = [], []

    def collect(container, label):
        for item in container or []:
            for f in item.get("facts", []) or []:
                if not f.get("verified"):
                    blocked.append(f["id"])
                    continue
                allowed[f["id"]] = {**f, "source": label, "parent": item.get("name") or item.get("company")}
                lines.append(f"{f['id']} [{item.get('name') or item.get('company')}] {f['text']}")

    collect(cfg.get("roles"), "role")
    collect(cfg.get("projects"), "project")
    return "\n".join(lines), {"allowed": allowed, "blocked": blocked}


def run(log=print, limit: int = 15) -> None:
    menu, meta = _menu()
    if not meta["allowed"]:
        log("! No verified facts. Open config/facts.yaml and flip `verified: true`")
        log(f"  on the ones you can defend in an interview. {len(meta['blocked'])} are waiting.")
        return
    if meta["blocked"]:
        log(f"note: {len(meta['blocked'])} unverified facts excluded: {', '.join(meta['blocked'][:8])}")

    rows = db.fetch(status="shortlisted", limit=limit)
    log(f"tailoring {len(rows)} shortlisted jobs")

    for job in rows:
        try:
            out = generate_json(
                PROMPT.format(
                    menu=menu, title=job["title"], company=job["company"],
                    description=(job["description"] or "")[:6000],
                    tailor_notes=job["tailor_notes"] or "none",
                ),
                model=MODEL_TAILOR, temperature=0.4,
            )
        except QuotaExhausted as e:
            log(f"! {e}")
            break
        except Exception as e:
            log(f"  tailor failed for {job['title'][:40]}: {type(e).__name__}")
            continue

        # ---- Validation gate: this is the whole point of the module ----
        kept, rejected = [], []
        for sel in out.get("selected", []):
            fid = sel.get("id")
            if fid in meta["allowed"]:
                kept.append({"id": fid, "text": sel.get("rewritten", ""),
                             "original": meta["allowed"][fid]["text"]})
            else:
                rejected.append(fid)

        if rejected:
            log(f"  ! dropped invented/unverified IDs for {job['company']}: {rejected}")
        if not kept:
            log(f"  ! nothing valid returned for {job['company']}, skipping")
            continue

        # Screening answers: one extra Gemini call per prepared job. Budget for
        # it -- these are the questions that actually eat the evening, and the
        # dashboard's prepared-doc panel has always implied they exist.
        screen = None
        try:
            screen = screening.generate_for(job)
        except QuotaExhausted:
            log("  ! budget spent before screening answers; resume prepared without them")
        except Exception as e:
            log(f"  screening failed for {job['company']}: {type(e).__name__}")

        doc = _render(job, out, kept, rejected, screen)
        path = OUT_DIR / f"{job['id']:05d}_{_slug(job['company'])}.md"
        path.write_text(doc)

        db.update(job["id"], status="prepared", resume_path=str(path),
                  fact_ids_used=json.dumps([k["id"] for k in kept]))
        log(f"  prepared: {job['company']} - {job['title'][:45]}")

    db.log_run("prepare", True)


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in s.lower())[:40].strip("-")


def _render(job, out, kept, rejected, screen: dict | None = None) -> str:
    lines = [
        f"# {job['title']}", f"**{job['company']}** - {job['location'] or 'n/a'}",
        f"Fit score: **{job['score']}** - {job['score_reason']}",
        f"\nApply: {job['apply_url'] or job['url']}",
        "\n---\n", "## Summary", out.get("summary", ""),
        "\n## Tailored bullets\n",
    ]
    for k in kept:
        lines.append(f"- {k['text']}")
        if k["text"].strip().lower() != k["original"].strip().lower():
            lines.append(f"  <sub>from {k['id']}: {k['original']}</sub>")
    lines += [
        "\n## Cover note\n", out.get("cover_note", ""),
        "\n## Be ready for this question\n", out.get("gap_honesty", ""),
    ]
    if screen and screen.get("answers"):
        lines += ["\n---\n", screening.render(screen)]
    elif screen and screen.get("error"):
        lines.append(f"\n> Screening answers unavailable: {screen['error']}")
    if rejected:
        lines.append(f"\n> Dropped unverified fact IDs: {', '.join(rejected)}")
    lines.append("\n---\n**You submit this yourself.** Nothing here has been sent anywhere.")
    return "\n".join(lines)
