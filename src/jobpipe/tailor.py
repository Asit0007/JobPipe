"""Resume tailoring, bounded by the facts file.

The model is handed a menu of fact IDs and must return a SELECTION. Every ID it
returns is validated against facts.yaml. Anything unverified or unrecognised is
dropped and reported. The model cannot add experience you do not have, because
it never gets to write the experience -- only choose among yours.

The ID gate is only half of it. It cannot see that a valid ID came back with a
rewrite that no longer says what the fact said, which is the one drift this
project has actually observed. `render.gate()` is the other half: it compares
each rewrite against its source fact and drops anything that introduces a
never_claim term. See claims.py.

Output is three files per job -- .md to audit, .json to regenerate from, .tex
to send. Only the .md and the LLM call are new work; render.py builds the rest.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import db, render, screening
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
  "summary": "<70 to 90 words, built only from selected facts. See SUMMARY RULES below>",
  "cover_note": "<4 sentences max, specific to this company, no flattery, no cliches>",
  "gap_honesty": "<1 sentence: the biggest genuine gap and how you would frame it if asked>"
}}

HARD RULES:
- Use 12 to 16 fact IDs. Never output an ID that is not in the menu above.
- "rewritten" must preserve the factual claim exactly. Change wording, never substance.
- Do not add metrics, percentages, team sizes, or durations that are not in the fact text.
  This includes shift patterns: an "L1/L2 on-call" fact does not say "24/7".
- Do not name a technology the fact does not name. Adding "and Linux" to a fact about
  Windows changes the claim even though every word is otherwise true.
- NO PERSONAL PRONOUNS anywhere -- no "I", "my", "me". Use implied-subject phrasing:
  "Administered RHEL fleets", never "I administered RHEL fleets".
- Where the menu offers a fact about coordinating with other teams, prefer it: one
  collaboration signal in the selection is worth more than a seventh solo bullet.

SHAPE OF THE SELECTION (this is what makes a resume read as strong rather than thin):
- Take 6 to 8 facts from the ROLE section. That is the paid experience and it leads.
  Cover more than one theme: systems administration, incident response, patching and
  vulnerability work, automation, disaster recovery, documentation, recognition. A
  resume showing one theme six times reads narrower than the person actually is.
- Then pick TWO OR THREE PROJECTS and take 2 to 3 facts from EACH. Never take a
  single fact from a project: one bullet under a project heading looks thin, and the
  heading costs two lines to carry one line of content. Either give a project 2-3
  facts or leave it out.
- Choose which projects by the "USE THIS ONE FOR" line under each heading, matched
  against this job. A project whose line does not match this JD should be skipped.
- Prefer facts that name a technology the JD names. This document is read by a
  keyword scanner before a human sees it, and an unused fact scores nothing.
- If the job is a poor match for the available facts, say so in gap_honesty rather
  than stretching the facts to fit.

SUMMARY RULES (these are calibrated against which resumes got interview callbacks):
- 70 to 90 words. Shorter starves keyword density and halves the match surface a
  scanner sees. Longer loses the reader.
- Name at least 10 searchable technical terms drawn from the selected facts.
- Structure: role and years, then core production work with a metric, then the
  self-built platform evidence with tool names, then certifications.
- Never write "applying for the X role" or any variation. Describe capability.
- No pronouns here either.
"""


def _menu() -> tuple[str, dict]:
    """Build the allowed-fact menu. Unverified facts are excluded entirely."""
    cfg = facts()
    allowed: dict[str, dict] = {}
    lines, blocked = [], []

    def collect(container, label):
        for item in container or []:
            parent = item.get("name") or item.get("company")
            # Group under a heading rather than emitting a flat list. The model
            # has to choose 2-3 bullets from the SAME project (the playbook's
            # rule), and it cannot cluster what it cannot see is clustered.
            header = f"\n## {label.upper()}: {parent}"
            if item.get("use_when"):
                header += f"\n   USE THIS ONE FOR: {item['use_when']}"
            lines.append(header)
            for f in item.get("facts", []) or []:
                if not f.get("verified"):
                    blocked.append(f["id"])
                    continue
                allowed[f["id"]] = {**f, "source": label, "parent": parent}
                lines.append(f"{f['id']} {f['text']}")

    collect(cfg.get("roles"), "role")
    collect(cfg.get("projects"), "project")
    return "\n".join(lines), {"allowed": allowed, "blocked": blocked}


def run(log=print, limit: int = 15, model: str | None = None) -> int:
    """Tailor the top `limit` shortlisted jobs. Returns how many were written.

    `model` overrides MODEL_TAILOR for this call. `cli daily` uses it to fall
    back to flash-lite when the better model is 503ing -- measured 2026-09-01,
    gemini-flash-latest returned 503 on all five retries and burned a quarter
    of its 20/day cap producing nothing. Returning the count is what lets the
    caller tell "the model is sick" from "there was nothing to do".
    """
    menu, meta = _menu()
    if not meta["allowed"]:
        log("! No verified facts. Open config/facts.yaml and flip `verified: true`")
        log(f"  on the ones you can defend in an interview. {len(meta['blocked'])} are waiting.")
        return 0
    if meta["blocked"]:
        log(f"note: {len(meta['blocked'])} unverified facts excluded: {', '.join(meta['blocked'][:8])}")

    rows = db.fetch(status="shortlisted", limit=limit)
    log(f"tailoring {len(rows)} shortlisted jobs")
    written = 0

    for job in rows:
        try:
            out = generate_json(
                PROMPT.format(
                    menu=menu, title=job["title"], company=job["company"],
                    description=(job["description"] or "")[:6000],
                    tailor_notes=job["tailor_notes"] or "none",
                ),
                model=model or MODEL_TAILOR, temperature=0.4,
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

        # ---- Second gate: never_claim. The ID above was real; this asks
        # whether the sentence built from it still is. ----
        kept, flags = render.gate(kept, {**out, "job": job})
        for d in flags.get("dropped", []):
            log(f"  ! never_claim dropped {d['id']} for {job['company']}: {d['reason'][:90]}")
        for d in flags.get("drift", []):
            bits = list(d.get("terms") or []) + [f"the number {n}" for n in d.get("numbers") or []]
            log(f"    drift {d['id']} introduced {', '.join(bits)} -- check the provenance")
        for d in flags.get("prose", []):
            log(f"  ! never_claim term in {d['field']} for {job['company']}: {', '.join(d['terms'])}")

        if not kept:
            log(f"  ! nothing valid returned for {job['company']}, skipping")
            continue

        # Screening answers: one extra Gemini call per prepared job. Budget for
        # it -- these are the questions that actually eat the evening, and the
        # dashboard's prepared-doc panel has always implied they exist.
        screen = None
        try:
            screen = screening.generate_for(job, model=model)
        except QuotaExhausted:
            log("  ! budget spent before screening answers; resume prepared without them")
        except Exception as e:
            log(f"  screening failed for {job['company']}: {type(e).__name__}")

        path = OUT_DIR / f"{job['id']:05d}_{_slug(job['company'])}.md"
        # Screening is a second call and can land on a different model than the
        # bullets -- so it is recorded separately, and left null when it failed.
        used = {"tailor": model or MODEL_TAILOR,
                "screening": (model or MODEL_TAILOR) if screen else None}
        path.write_text(_render(job, out, kept, rejected, screen, flags, models=used))

        # The payload is stored so the .tex can be rebuilt for free. Re-running
        # prepare to change a layout would cost 2 of the tailor model's 20
        # daily calls per job (CLAUDE.md 7.33); `cli tex` costs nothing.
        doc = render.payload(job, out, kept, flags, screen, models=used)
        render.write_sidecar(doc, path)
        tex_path = render.write_tex(doc, path)

        db.update(job["id"], status="prepared", resume_path=str(path),
                  fact_ids_used=json.dumps([k["id"] for k in kept]))
        written += 1
        log(f"  prepared: {job['company']} - {job['title'][:45]}")
        log(f"    {path.name} (audit) + {tex_path.name} (send)")

    return written


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in s.lower())[:40].strip("-")


def _render(job, out, kept, rejected, screen: dict | None = None, flags: dict | None = None,
            models: dict | None = None) -> str:
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
    lines += _flag_lines(flags or {})
    if models:
        t, s = models.get("tailor"), models.get("screening")
        lines.append(f"\n<sub>bullets by {t or 'unknown'}"
                     + (f"; screening by {s}" if s and s != t else "")
                     + "</sub>")
    lines.append("\n---\n**You submit this yourself.** Nothing here has been sent anywhere.")
    return "\n".join(lines)


def _flag_lines(flags: dict) -> list[str]:
    """The never_claim gate, rendered where the human will actually read it."""
    lines: list[str] = []
    if flags.get("dropped"):
        lines.append("\n## Dropped by the never_claim gate\n")
        for d in flags["dropped"]:
            lines.append(f"- **{d['id']}** - {d['reason']}")
            lines.append(f"  <sub>was: {d['text']}</sub>")
    if flags.get("drift"):
        lines.append("\n## Check these against their source fact\n")
        lines.append("These bullets state a technology or a number their fact does not. "
                     "Not forbidden, but this is the shape the one real drift took, and "
                     "an invented metric is a question you cannot answer.\n")
        for d in flags["drift"]:
            bits = list(d.get("terms") or []) + [f"the number {n}" for n in d.get("numbers") or []]
            lines.append(f"- **{d['id']}** introduced: {', '.join(bits)}")
    if flags.get("prose"):
        lines.append("\n## never_claim terms in free prose\n")
        for d in flags["prose"]:
            lines.append(f"- **{d['field']}**: {', '.join(d['terms'])}")
    return lines
