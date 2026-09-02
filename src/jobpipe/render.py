"""Turn a prepared application into the file you send.

`prepare` writes three artefacts per job and they do different jobs:

    .md    the audit copy. Carries `from Fxxx:` under every bullet -- CLAUDE.md
           calls that the facts gate's real second half, and a PDF cannot show it.
    .json  the validated payload, so the .tex can be rebuilt without an LLM call.
    .tex   the document. Compiles to the PDF you attach.

Regeneration is free and offline. That matters: `MODEL_TAILOR` allows 20 calls
a day and `prepare` spends two per job, so re-running `prepare` to change a
layout would cost a day's throughput. `jobpipe.cli tex` rebuilds instead.

For documents prepared before .json existed, the .md is parsed back -- its
shape is fixed because this repo wrote it.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from . import claims, latexdoc
from .config import OUT_DIR, facts

# Engines in preference order. Tectonic first: self-contained, downloads only
# the packages the document needs, and container-friendly -- which matters
# because 7.12 pulled ~500 MB of texlive back out of the Dockerfile.
ENGINES = ("tectonic", "latexmk", "pdflatex", "xelatex")


# --------------------------------------------------------------------------
# payload: build, store, recover
# --------------------------------------------------------------------------
def payload(job, out: dict, bullets: list[dict], flags: dict, screen: dict | None,
            models: dict | None = None) -> dict:
    """The stored payload. `models` names the model behind each half.

    Tailoring and screening are separate calls and can land on different
    models: `daily` falls back to flash-lite when MODEL_TAILOR is 503ing, and
    `rescreen` re-answers screening long after the bullets were written. On
    2026-09-02 both happened at once and no artifact could say which model had
    produced which text -- while the bullets carry `from Fxxx:` provenance
    precisely so drift is traceable, and weaker models drift more.
    """
    return {
        "job": {k: job[k] for k in ("id", "title", "company", "location", "description",
                                    "score", "score_reason", "apply_url", "url")},
        "summary": out.get("summary", ""),
        "cover_note": out.get("cover_note", ""),
        "gap_honesty": out.get("gap_honesty", ""),
        "bullets": bullets,
        "flags": flags,
        "screening": screen,
        "models": models or {},
    }


def sidecar(md_path: Path) -> Path:
    return md_path.with_suffix(".json")


def load(md_path: Path) -> dict:
    """The stored payload, or one reconstructed from the markdown."""
    side = sidecar(md_path)
    if side.exists():
        return json.loads(side.read_text())
    return parse_markdown(md_path.read_text(), md_path)


_SECTION = re.compile(r"^## (.+)$", re.M)
_SUB = re.compile(r"^\s*<sub>from ([A-Z]\d{3}): (.+?)</sub>\s*$")


def parse_markdown(text: str, path: Path | None = None, cfg: dict | None = None) -> dict:
    """Recover a payload from a document this repo wrote.

    A bullet only carries its `<sub>from Fxxx:` line when the rewrite differs
    from the fact, so an unannotated bullet is matched back to facts.yaml by
    its text. A bullet we cannot source is kept and reported rather than
    dropped -- it is in a document the user may already have sent, and a
    silent deletion is the worst outcome available here.
    """
    cfg = cfg or facts()
    by_text = {}
    for section in ("roles", "projects"):
        for group in cfg.get(section) or []:
            for f in group.get("facts") or []:
                by_text[" ".join(f["text"].lower().split())] = f["id"]

    parts: dict[str, str] = {}
    marks = list(_SECTION.finditer(text))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        parts[m.group(1).strip().lower()] = text[m.end():end].strip()

    head = text[: marks[0].start()] if marks else text
    title = (re.search(r"^# (.+)$", head, re.M) or [None, ""])[1].strip()
    company_line = re.search(r"^\*\*(.+?)\*\*\s*-\s*(.*)$", head, re.M)
    score = re.search(r"^Fit score: \*\*(\d+)\*\*\s*-\s*(.*)$", head, re.M)
    apply_url = (re.search(r"^Apply:\s*(\S+)", head, re.M) or [None, ""])[1]

    bullets, unsourced = [], []
    lines = parts.get("tailored bullets", "").splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("- "):
            continue
        body = line[2:].strip()
        sub = _SUB.match(lines[i + 1]) if i + 1 < len(lines) else None
        if sub:
            bullets.append({"id": sub.group(1), "text": body, "original": sub.group(2).strip()})
            continue
        fid = by_text.get(" ".join(body.lower().split()))
        if fid:
            bullets.append({"id": fid, "text": body, "original": body})
        else:
            unsourced.append(body)

    doc = {
        "job": {
            "id": int(path.name.split("_")[0]) if path and path.name[:1].isdigit() else 0,
            "title": title,
            "company": company_line.group(1) if company_line else "",
            "location": company_line.group(2).strip() if company_line else "",
            "description": "",
            "score": int(score.group(1)) if score else None,
            "score_reason": score.group(2).strip() if score else "",
            "apply_url": apply_url, "url": apply_url,
        },
        "summary": parts.get("summary", ""),
        "cover_note": parts.get("cover note", ""),
        "gap_honesty": parts.get("be ready for this question", ""),
        "bullets": bullets,
        "flags": {"unsourced": unsourced} if unsourced else {},
        "screening": None,
        "models": {},
    }
    return doc


# --------------------------------------------------------------------------
# the never_claim gate
# --------------------------------------------------------------------------
def _field(row, key: str) -> str:
    """Read one column from either a dict or a sqlite3.Row.

    `prepare` hands the live DB row straight through; tests and the markdown
    round-trip hand a plain dict. sqlite3.Row supports [] but not .get(), so
    assuming either shape breaks the other -- and it broke the real path while
    every test passed.
    """
    try:
        return str(row[key] or "")
    except (KeyError, IndexError, TypeError):
        return ""


def _context(fid: str, cfg: dict) -> str:
    """A fact's text plus what legitimately surrounds it.

    A CloudPulse bullet may say "Docker" because Docker is in CloudPulse's
    stack; that is not drift. Anything outside this string is something the
    rewrite introduced on its own.
    """
    for section in ("roles", "projects"):
        for group in cfg.get(section) or []:
            for f in group.get("facts") or []:
                if f["id"] == fid:
                    return " ".join([
                        f["text"], str(group.get("name") or group.get("company") or ""),
                        " ".join(group.get("stack") or []), " ".join(f.get("tags") or []),
                    ])
    return ""


def gate(bullets: list[dict], out: dict, cfg: dict | None = None) -> tuple[list[dict], dict]:
    """never_claim + drift, applied to everything the model wrote.

    Severity is deliberately split. A bullet that introduces a forbidden claim
    is DROPPED: there are six to nine of them and losing one costs nothing next
    to sending a claim the user cannot defend in an interview. The summary and
    cover note are FLAGGED instead, because dropping them empties a required
    section -- and unlike a bullet they have no single source fact to check
    against, so a match there is likelier to be a false positive. Both are
    written into the .md and the .tex; neither is silent.

    A number is treated like a technology: stated by the rewrite, absent from
    the source fact, therefore introduced. RESUME_PLAYBOOK.md section 5 lists
    the approved metrics and says "never invent others" -- this is the half of
    that a machine can check. Measured over the nine prepared documents, 1 of 69
    bullets tripped it, and it was real: an "L1/L2 on-call engineer" fact came
    back as "24/7 on-call support", which is a shift pattern nobody wrote down.
    """
    cfg = cfg or facts()
    kept, dropped, drift = [], [], []
    for b in bullets:
        source = _context(b["id"], cfg) or b.get("original", "")
        found = claims.audit(b.get("text", ""), source, cfg)
        if found["forbidden"]:
            terms = ", ".join(sorted({f["term"] for f in found["forbidden"]}))
            rules = sorted({f["rule"] for f in found["forbidden"]})
            dropped.append({"id": b["id"], "text": b.get("text", ""),
                            "reason": f"introduced {terms} -- never_claim: {rules[0]}"})
            continue
        if found["drifted"] or found["invented_numbers"]:
            drift.append({"id": b["id"], "terms": found["drifted"],
                          "numbers": found["invented_numbers"]})
        kept.append(b)

    # Prose has no single source fact, so it is checked against everything that
    # survived, plus the skills the user maintains by hand -- that list IS what
    # they claim, and most never_claim rules forbid a technology in a specific
    # context ("Python at <employer>") that a summary is not asserting.
    #
    # The employer's own name goes in too. Measured on the nine prepared
    # documents, the single false positive in the batch was "GitLab" in a cover
    # note addressed to GitLab: the rule forbids GitLab CI, and refusing to let
    # the letter name the company it is addressed to is worse than useless.
    # Bullets get no such allowance -- a resume bullet never names the employer.
    job = out.get("job") or {}
    corpus = " ".join([_context(b["id"], cfg) for b in kept]
                      + [b.get("text", "") for b in kept]
                      + [_field(job, "company"), _field(job, "title")]
                      + [s for g in (cfg.get("skills") or {}).values() for s in (g or [])])
    # `gap_honesty` is deliberately NOT audited. Its entire job is to name the
    # technology the candidate does not have -- "the primary gap is direct
    # production management of Kubernetes and EKS" is the field working
    # correctly, not a forbidden claim. Measured: 2 of the 4 prose flags in the
    # first real batch were gap statements doing exactly what they should, and
    # flagging them would push the model toward vaguer, less honest gaps.
    prose = []
    for field in ("summary", "cover_note"):
        found = claims.audit(out.get(field, "") or "", corpus, cfg)
        if found["forbidden"]:
            prose.append({"field": field,
                          "terms": sorted({f["term"] for f in found["forbidden"]})})
    flags = {}
    if dropped:
        flags["dropped"] = dropped
    if drift:
        flags["drift"] = drift
    if prose:
        flags["prose"] = prose
    return kept, flags


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------
def write_tex(doc: dict, md_path: Path) -> Path:
    path = md_path.with_suffix(".tex")
    path.write_text(latexdoc.render(doc))
    return path


def write_sidecar(doc: dict, md_path: Path) -> Path:
    path = sidecar(md_path)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False))
    return path


def documents(target: str | None = None) -> list[Path]:
    """Prepared markdown documents, optionally narrowed to one job id."""
    docs = sorted(OUT_DIR.glob("[0-9]*.md"))
    if target and target != "all":
        want = f"{int(target):05d}_"
        docs = [d for d in docs if d.name.startswith(want)]
    return docs


# --------------------------------------------------------------------------
# pdf
# --------------------------------------------------------------------------
def find_engine() -> str | None:
    return next((e for e in ENGINES if shutil.which(e)), None)


def compile_pdf(tex_path: Path, engine: str | None = None,
                keep_logs: bool = False) -> tuple[bool, str, int | None]:
    """Compile in place. Returns (ok, message, pages)."""
    engine = engine or find_engine()
    if not engine:
        return False, ("no TeX engine on PATH. `brew install tectonic` is the "
                       "lightest option (single binary, fetches only what the "
                       "document needs)"), None
    out = tex_path.parent
    cmds = {
        # --keep-logs so the page count can be read back; the log is removed below.
        "tectonic": [["tectonic", "-o", str(out), "--keep-logs", str(tex_path)]],
        "latexmk": [["latexmk", "-pdf", "-interaction=nonstopmode",
                     f"-outdir={out}", str(tex_path)]],
        # A single pass leaves \hfill positions and hyperref anchors unresolved.
        "pdflatex": [["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
                      "-output-directory", str(out), str(tex_path)]] * 2,
        "xelatex": [["xelatex", "-interaction=nonstopmode", "-halt-on-error",
                     "-output-directory", str(out), str(tex_path)]] * 2,
    }[engine]
    for cmd in cmds:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=out)
        if proc.returncode != 0:
            tail = _first_error(proc.stdout + proc.stderr)
            return False, f"{engine} failed: {tail}", None
    pdf = tex_path.with_suffix(".pdf")
    pages = _pages(tex_path.with_suffix(".log"))
    if not keep_logs:
        for junk in (".log", ".aux", ".out", ".fls", ".fdb_latexmk", ".synctex.gz"):
            tex_path.with_suffix(junk).unlink(missing_ok=True)
    if not pdf.exists():
        return False, f"{engine} ran but wrote no PDF", None
    return True, str(pdf), pages


_PAGES = re.compile(r"Output written on .*?\((\d+) pages?", re.S)


def _pages(log_path: Path) -> int | None:
    """The engine already counted. Every TeX engine writes this line, so the
    template's "[ ] Fits on ONE page" stops being a thing you eyeball."""
    try:
        return int(_PAGES.search(log_path.read_text(errors="replace")).group(1))
    except (OSError, AttributeError):
        return None


def _first_error(log: str) -> str:
    """LaTeX logs are long and the useful line is near the first '!'."""
    lines = log.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("!") or "error:" in line.lower():
            return " | ".join(x.strip() for x in lines[i:i + 3] if x.strip())[:300]
    return " ".join(lines[-3:])[:300]
