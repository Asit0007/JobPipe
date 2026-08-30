"""Markdown is for auditing. This is the file you actually send.

`Asit_Minz_TEMPLATE.tex` is not a layout -- it carries rules: a tagline that is
a role descriptor and not the job title, skill rows reordered to the JD, no
"personal projects" caveat inside a skill row, no arrow/pipe/greater-than in
skill or bullet content, the Magento repo URL suppressed while blocker B001 is
open, and a pre-send checklist. A generator that only fills macros produces a
document that compiles and is still wrong. This module encodes the rules.

Everything here is DERIVED -- no LLM call. That is deliberate on two counts:

  * `MODEL_TAILOR` allows 20 requests a day (CLAUDE.md 7.33), and `prepare`
    already spends two of them per job. A third would cut throughput by a third.
  * A tagline and a skill row are selections from lists the user maintains.
    Asking a model to write them would reopen exactly the hole facts.yaml closes.

So the .tex can be regenerated from a prepared document at any time, for free.
"""
from __future__ import annotations

import re

from .config import facts

# --------------------------------------------------------------------------
# escaping
# --------------------------------------------------------------------------
_ESCAPES = {
    "\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "$": r"\$", "&": r"\&",
    "#": r"\#", "_": r"\_", "%": r"\%", "^": r"\textasciicircum{}",
    "~": r"\textasciitilde{}",
}
# pdflatex with inputenc handles Latin-1 fine; these are the characters an LLM
# actually emits that either break the compile or look wrong in the PDF.
_UNICODE = {
    "—": "---", "–": "--", "‘": "`", "’": "'",
    "“": "``", "”": "''", "…": r"\ldots{}", "•": r"\textbullet{}",
    " ": " ", "−": "-", "×": r"$\times$", "≥": r"$\geq$",
    "≤": r"$\leq$", "±": r"$\pm$", "→": ", ", "➔": ", ",
    "­": "",
}
_SPLITTER = re.compile(r"(?<=\s)(?:->|-->|→|>|\|)(?=\s)")


def escape(text: str) -> str:
    r"""LaTeX-escape arbitrary model output.

    A `.replace()` chain is not enough, in both directions. `\` has to be
    substituted before the characters its own replacement introduces, or `\%`
    becomes `\textbackslash{}\%`; and a Unicode substitution that emits LaTeX
    (`\times` for the multiplication sign) must not then be escaped in turn.
    One pass, one lookup per character, and every replacement final.
    """
    if not text:
        return ""
    out = []
    for ch in text:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ch in _UNICODE:
            out.append(_UNICODE[ch])
        elif ord(ch) > 0xFF and ch not in "\n\t":
            # pdflatex + inputenc covers Latin-1. Anything past it ("Unicode
            # character not set up for use with LaTeX") kills the compile, and
            # a job description will eventually contain one.
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def ats_clean(text: str) -> str:
    """Strip the separators the template forbids in skill and bullet content.

    "Scanners flag mixed bullet symbols." Arrows, pipes and greater-than signs
    used as separators become commas; `$|$` survives only in \\jobheader and the
    header tagline, which build it themselves.
    """
    text = _SPLITTER.sub(",", text or "")
    return re.sub(r"\s+,", ",", re.sub(r",\s*,", ",", text)).strip()


def tex(text: str) -> str:
    """The normal path for any model-written string."""
    return escape(ats_clean(text))


def comment(text: str, indent: str = "%   ") -> str:
    """Fold arbitrary text into LaTeX comment lines.

    A newline inside a comment ends the comment and dumps the rest into the
    document, so this is not optional.
    """
    flat = " ".join(str(text or "").split())
    return "\n".join(indent + flat[i:i + 96] for i in range(0, len(flat), 96)) or indent


# --------------------------------------------------------------------------
# skills: which rows, in what order
# --------------------------------------------------------------------------
# Every entry in facts.yaml `skills` maps to exactly one row. Anything the user
# adds later that is not listed here still appears -- in a trailing row, with a
# note in the .tex -- rather than vanishing.
SKILL_ROWS: list[tuple[str, tuple[str, ...]]] = [
    ("Cloud Platforms", ("AWS", "Azure", "OCI", "DigitalOcean", "Vercel", "Cloudflare Tunnel")),
    ("Infrastructure as Code", ("Terraform", "HashiCorp Vault")),
    ("Containers", ("Docker", "Docker Compose", "Kubernetes")),
    ("CI/CD and Version Control", ("GitHub Actions", "Git")),
    ("Operating Systems", ("Linux (RHEL/CentOS)", "Windows Server")),
    ("Scripting and Languages", ("Bash", "PowerShell", "Python", "Go")),
    ("Web and Data Services", ("NGINX", "Varnish", "MySQL", "Elasticsearch", "React")),
    ("Virtualization", ("VMware",)),
]

MAX_SKILL_ROWS = 9

_ROLE_DESCRIPTORS: list[tuple[tuple[str, ...], str]] = [
    (("site reliability", "sre"), "Site Reliability Engineer"),
    (("devops", "dev ops"), "DevOps Engineer"),
    (("platform engineer", "platform"), "Platform Engineer"),
    (("cloud",), "Cloud Engineer"),
    (("linux", "unix", "system administrator", "systems administrator", "sysadmin"),
     "Systems Engineer"),
    (("infrastructure", "infra"), "Infrastructure Engineer"),
    (("automation", "build", "release"), "Automation Engineer"),
]
_AI_TERMS = ("genai", "generative ai", " ai ", "aiops", "llm", "machine learning")

# RESUME_PLAYBOOK.md 7a: "No personal pronouns. Never I, my, me." Implied-subject
# phrasing throughout. `I` is matched case-sensitively -- "i" is a variable name,
# a roman numeral and the middle of nothing.
_PRONOUNS = (re.compile(r"(?<![\w-])I(?![\w-])"),
             re.compile(r"(?<![\w-])(?:my|me|mine|myself)(?![\w-])", re.I))

# 7a again: "scanners look for collaboration language", with four honest sources
# of it listed. This is the vocabulary those four use.
_TEAMWORK = re.compile(
    r"(?<![\w-])(?:coordinat\w*|collaborat\w*|alongside|cross-team|cross-functional|"
    r"stakeholder\w*|escalat\w*|partner\w*|liais\w*|team|teams|jointly|"
    r"together with)(?![\w-])", re.I)


def pronouns_in(text: str) -> bool:
    return any(p.search(text or "") for p in _PRONOUNS)


def _display(skill: str) -> str:
    """"Linux (RHEL/CentOS)" -> "Linux (RHEL/CentOS)"; for the tagline, "Linux"."""
    return re.sub(r"\s*\([^)]*\)", "", skill).strip()


def _mentions(term: str, jd: str) -> int:
    pieces = [p for p in re.split(r"[/,()]", term) if len(p.strip()) >= 2]
    flags = 0 if len(term) <= 3 else re.IGNORECASE
    return sum(len(re.findall(rf"(?<![\w-]){re.escape(p.strip())}(?![\w-])", jd, flags))
               for p in pieces)


def skill_rows(jd: str, cfg: dict) -> tuple[list[tuple[str, str]], list[str]]:
    """Rows ordered by what this JD asks for. Returns (rows, uncategorised).

    The rows come from `facts.yaml: skill_rows`, which is the master resume's
    own inventory. The generator used to synthesise them from the bare `skills`
    lists, which produced "Infrastructure as Code: Terraform" where the master
    produces a sentence naming ten more searchable terms. An ATS reads the
    second one very differently, and the master is the user's own writing.

    Ordering is by JD demand, per the template's rule that the JD's top
    priorities appear first. Rows are NOT dropped for ranking low: this is the
    keyword surface a scanner matches against, and a DevOps posting that never
    spells out "Bash" still wants the person who writes it.
    """
    rows_cfg = cfg.get("skill_rows") or []
    if not rows_cfg:
        return _legacy_skill_rows(jd, cfg)

    ranked = []
    for row in rows_cfg:
        when = str(row.get("use_when") or "")
        if when and not any(t in jd.lower() for t in _AI_TERMS):
            continue
        weight = sum(_mentions(m, jd) for m in (row.get("match") or []))
        ranked.append((weight, row))
    # Stable: equal-weight rows keep the master's own order, which is not random.
    ranked.sort(key=lambda r: -r[0])
    return [(r["label"], r["text"]) for _, r in ranked[:MAX_SKILL_ROWS]], []


def _legacy_skill_rows(jd: str, cfg: dict) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """Synthesise rows from the bare `skills` lists.

    Only reached when facts.yaml carries no `skill_rows` block -- a config
    written before 2026-08-30, or the example template. Kept so the generator
    still produces something sane rather than an empty Skills section.
    """
    skills = cfg.get("skills") or {}
    claimable = list(skills.get("strong") or []) + list(skills.get("working") or [])
    exposure = [s for s in (skills.get("exposure") or []) if _mentions(s, jd)]
    included = claimable + exposure
    placed: set[str] = set()

    rows: list[tuple[str, list[str], int]] = []
    for label, members in SKILL_ROWS:
        present = [s for s in included if s in members]
        placed.update(present)
        if present:
            present.sort(key=lambda s: -_mentions(s, jd))
            rows.append((label, present, sum(_mentions(s, jd) for s in present)))
    rows.sort(key=lambda r: -r[2])
    leftover = [s for s in included if s not in placed]

    strong = set(skills.get("strong") or [])
    protected = [r for r in rows if any(s in strong for s in r[1])]
    if len(rows) > MAX_SKILL_ROWS and [r for r in rows if r not in protected]:
        keep = MAX_SKILL_ROWS - 1
        surviving = [r for r in rows if r in protected or rows.index(r) < keep]
        dropped = [r for r in rows if r not in surviving]
        if len(dropped) >= 2:
            rows = surviving[:MAX_SKILL_ROWS] + [
                ("Also", [s for _, items, _ in dropped for s in items], 0)]
    return [(label, items) for label, items, _ in rows[:9]], leftover


def tagline(job_title: str, jd: str, rows: list[tuple[str, list[str]]],
            cfg: dict | None = None) -> tuple[str, list[str]]:
    """Role descriptor plus the JD's own vocabulary, drawn from the skill rows.

    A never_claim technology is filtered out here even though it is allowed in a
    skill row. The distinction is deliberate and it is the template's: a skill
    row is a list the user maintains, while six words under your name read as
    your core competencies. Kubernetes belongs in facts.yaml `exposure` -- the
    user put it there -- and does not belong in a headline that says "core
    kubectl commands only" three lines further down in the same file.
    """
    from .claims import never_claim_terms
    cfg = cfg or facts()
    forbidden = {t.lower() for t, _ in never_claim_terms(cfg)}
    title = (job_title or "").lower()
    descriptor = next(
        (name for needles, name in _ROLE_DESCRIPTORS if any(n in title for n in needles)),
        "Infrastructure Engineer")
    pool: list[str] = []
    for _, items in rows:
        if isinstance(items, str):
            # Prose row: take the technology names the JD also mentions.
            pool += [t for t in re.split(r"[,;]", items) if 0 < len(t.strip()) <= 28]
        else:
            pool += items
    vocab = [s for g in (cfg.get("skills") or {}).values() for s in (g or [])]
    pool = [v for v in vocab if _mentions(v, " ".join(pool))] or pool
    ranked = sorted(pool, key=lambda s: -_mentions(s, jd))
    keywords, seen = [], set()
    for skill in ranked:
        label = _display(skill).strip()
        if label.lower() in seen or label.lower() in forbidden:
            continue
        seen.add(label.lower())
        keywords.append(label)
    return descriptor, keywords[:6]


# --------------------------------------------------------------------------
# document
# --------------------------------------------------------------------------
SEP = r" \textbullet\ "      # the template's separator; defined once so it never has
                                 # to survive being escaped as data

PREAMBLE = r"""\documentclass[10pt, letterpaper]{article}
\usepackage[top=0.55in, bottom=0.55in, left=0.70in, right=0.70in]{geometry}
\usepackage{enumitem}
\usepackage{titlesec}
\usepackage[hidelinks]{hyperref}
\usepackage{xcolor}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{microtype}
\usepackage{parskip}

\definecolor{darkblue}{RGB}{26, 58, 92}
\definecolor{midblue}{RGB}{31, 111, 180}
\definecolor{darkgrey}{RGB}{43, 43, 43}
\definecolor{lightgrey}{RGB}{85, 85, 85}
\renewcommand{\familydefault}{\sfdefault}
\color{darkgrey}
\pagestyle{empty}
\titleformat{\section}{\large\bfseries\color{darkblue}}{}{0em}{}[\color{midblue}\titlerule]
\titlespacing{\section}{0pt}{8pt}{4pt}
\setlist[itemize]{leftmargin=1.5em, itemsep=1pt, parsep=0pt, topsep=2pt, label=\textbullet}
% \jobheader differs from the template by one control space, and it is a
% fix, not a preference. In the template the divider is written as
% \textcolor{lightgrey}{ $|$ #2} -- and TeX skips spaces while scanning an
% undelimited macro argument, so that leading space is eaten and every
% resume built from the template reads "Technology Operations| Employer".
% Verified by compiling the macro on its own. Moving the space outside the
% argument restores it.
\newcommand{\jobheader}[3]{\vspace{4pt}\noindent\textbf{\color{darkblue}#1}\ \textcolor{lightgrey}{$|$ #2}\hfill\textbf{\small\color{darkblue}#3}\par\vspace{1pt}}
\newcommand{\projheader}[3]{\vspace{4pt}\noindent\textbf{\color{darkblue}#1}\hfill\textbf{\small\color{darkblue}#3}\par\vspace{1pt}\noindent\textit{\small\textcolor{lightgrey}{#2}}\par\vspace{1pt}}
\newcommand{\skillrow}[2]{\noindent\textbf{\color{darkblue}#1:} #2\par\vspace{1pt}}
"""


def _index(cfg: dict) -> dict:
    """fact id -> {group, section, fact}."""
    idx = {}
    for section in ("roles", "projects"):
        for group in cfg.get(section) or []:
            for f in group.get("facts") or []:
                idx[f["id"]] = {"group": group, "section": section, "fact": f}
    return idx


def _period(role: dict) -> str:
    def fmt(v):
        v = str(v or "")
        if v.lower() == "present":
            return "Present"
        try:
            y, m = v.split("-")
            return f"{['','Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][int(m)]} {y}"
        except (ValueError, IndexError):
            return v
    return f"{fmt(role.get('start'))} -- {fmt(role.get('end'))}"


def _is_award(fact: dict) -> bool:
    return bool({"award", "recognition"} & set(fact.get("tags") or []))


def render(doc: dict, cfg: dict | None = None) -> str:
    """Build the .tex. `doc` is the payload tailor.py validated -- see render.py."""
    cfg = cfg or facts()
    meta = cfg.get("meta") or {}
    job = doc["job"]
    jd = (job.get("description") or "") + " " + (job.get("title") or "")
    idx = _index(cfg)

    rows, leftover = skill_rows(jd, cfg)
    descriptor, keywords = tagline(job.get("title", ""), jd, rows, cfg)

    L: list[str] = [
        "% " + "=" * 74,
        f"%  {meta.get('name', 'Resume')} -- {job.get('company', '')} / {job.get('title', '')}",
        "%  Generated by jobpipe from config/facts.yaml. Every bullet is a verified",
        "%  fact; the provenance map is at the bottom of this file and in the .md.",
        "%  Read the CHECKLIST at the end before you send this.",
        "% " + "=" * 74,
        PREAMBLE,
        r"\begin{document}",
        "",
        "% --- HEADER ------------------------------------------------------------",
        r"\begin{center}",
        rf"  {{\LARGE\bfseries\color{{darkblue}} {escape(meta.get('name', ''))}}}\\[4pt]",
        r"  {\small\textit{\color{lightgrey} " + escape(descriptor) + r" $|$ "
        + r" \textbullet\ ".join(escape(k) for k in keywords) + r"}}\\[3pt]",
        r"  {\small\color{lightgrey} " + escape(meta.get("location", ""))
        + r" \textbullet\ " + escape(meta.get("phone", "")),
    ]
    for url, label in ((meta.get("email"), meta.get("email")),
                       (meta.get("portfolio"), meta.get("portfolio")),
                       (meta.get("linkedin"), meta.get("linkedin")),
                       (meta.get("github"), meta.get("github"))):
        if url:
            href = f"mailto:{url}" if "@" in str(url) else str(url)
            shown = re.sub(r"^https?://", "", str(label))
            L.append(rf"    \textbullet\ \href{{{escape(href)}}}{{{escape(shown)}}}")
    L += [r"  }", r"\end{center}",
          r"{\color{darkblue}\rule{\linewidth}{1.5pt}}\vspace{2pt}", ""]

    # --- summary ---
    words = len((doc.get("summary") or "").split())
    L += ["% --- PROFESSIONAL SUMMARY ---------------------------------------------",
          f"%  {words} words. The template calls for 70-90: under 70 starves keyword",
          "%  density, over 90 loses the reader. Expand from facts.yaml, never from",
          "%  memory." if words < 70 else "%  Length is within the template's 70-90 word band.",
          r"\section{Professional Summary}",
          tex(doc.get("summary", "")), ""]

    # --- skills ---
    L += ["% --- TECHNICAL SKILLS -------------------------------------------------",
          "%  Rows ordered by what this JD asks for. `exposure` skills appear only",
          "%  when the JD names them -- omitted silently, never caveated.",
          r"\section{Technical Skills}"]
    for label, items in rows:
        body = items if isinstance(items, str) else ", ".join(_display(s) for s in items)
        L.append(rf"\skillrow{{{tex(label)}}}{{{tex(body)}}}")
    if leftover:
        L.append(rf"\skillrow{{Additional}}{{{tex(', '.join(_display(s) for s in leftover))}}}")
        L.append("%  ^ these skills are in facts.yaml but not in latexdoc.SKILL_ROWS.")
    L.append("")

    # --- experience ---
    by_group: dict[int, list[dict]] = {}
    for b in doc.get("bullets", []):
        entry = idx.get(b["id"])
        if entry:
            by_group.setdefault(id(entry["group"]), []).append(b)

    L += ["% --- PROFESSIONAL EXPERIENCE ------------------------------------------",
          r"\section{Professional Experience}"]
    for role in cfg.get("roles") or []:
        picked = by_group.get(id(role), [])
        items: list[str] = []
        if role.get("progression"):
            items.append(("progression", _progression_line(role)))
        items += [(b["id"], b["text"]) for b in picked if not _is_award(idx[b["id"]]["fact"])]
        items += [(b["id"], b["text"]) for b in picked if _is_award(idx[b["id"]]["fact"])]
        if not items:
            continue
        L.append(rf"\jobheader{{{tex(role.get('title', ''))}}}"
                 rf"{{{tex(role.get('company', ''))}, {tex(role.get('location', ''))}}}"
                 rf"{{{tex(_period(role))}}}")
        L.append(r"\begin{itemize}")
        for fid, text in items:
            L.append(f"  % {fid}")
            L.append(rf"  \item {tex(text)}")
        L.append(r"\end{itemize}")
    L.append("")

    # --- projects ---
    projects = [p for p in (cfg.get("projects") or []) if by_group.get(id(p))]
    projects.sort(key=lambda p: (-len(by_group[id(p)]),
                                 -sum(_mentions(s, jd) for s in (p.get("stack") or []))))
    if projects:
        L += ["% --- PROJECTS ---------------------------------------------------------",
              "%  Ordered by JD relevance, not chronology. A project URL is printed only",
              "%  when facts.yaml carries one -- Magento_DeployKit's is blank on purpose",
              "%  while blocker B001 (credentials in public history) is open.",
              r"\section{Projects}"]
        for proj in projects:
            stack = sorted(proj.get("stack") or [], key=lambda s: -_mentions(s, jd))[:6]
            # Escape each item, then join with the separator -- joining first
            # would send the separator's own backslashes through the escaper.
            stack_tex = SEP.join(tex(_display(s)) for s in stack)
            L.append(rf"\projheader{{{tex(proj.get('name', ''))}}}{{{stack_tex}}}"
                     rf"{{{tex(proj.get('year', ''))}}}")
            if proj.get("url"):
                # Commented, as in the template: the header already carries the
                # GitHub profile, and a line per project is a line off the page.
                # Uncomment when a specific repo is worth linking directly.
                L.append(rf"% \noindent\href{{{escape(proj['url'])}}}"
                         rf"{{\small\textcolor{{midblue}}"
                         rf"{{{escape(re.sub(r'^https?://', '', proj['url']))}}}}}\par")
            L.append(r"\begin{itemize}")
            for b in by_group[id(proj)]:
                L.append(f"  % {b['id']}")
                L.append(rf"  \item {tex(b['text'])}")
            L.append(r"\end{itemize}")
        L.append("")

    # --- education ---
    for edu in cfg.get("education") or []:
        if not edu.get("verified", True):
            continue
        course = sorted(edu.get("coursework") or [], key=lambda c: -_mentions(c, jd))[:5]
        L += ["% --- EDUCATION --------------------------------------------------------",
              r"\section{Education}",
              rf"\jobheader{{{tex(edu.get('degree', ''))}}}{{{tex(edu.get('institution', ''))}}}"
              rf"{{Graduated {tex(edu.get('year', ''))}}}"]
        if course:
            L.append(rf"\noindent\small Relevant Coursework: {tex(', '.join(course))}")
        L.append("")

    # --- certifications ---
    certs, held = _certifications(cfg, jd)
    if certs:
        L += ["% --- CERTIFICATIONS ---------------------------------------------------",
              r"\section{Certifications}", r"\begin{itemize}"]
        for c in certs:
            suffix = "" if c.get("status") == "complete" else f", {c.get('status', '')}"
            L.append(rf"  \item {tex(c.get('name', ''))}{tex(suffix)}")
        for c in held:
            L.append(rf"  % \item {tex(c.get('name', ''))}   "
                     rf"<- held back to keep this to one page; swap in if it fits")
        L += [r"\end{itemize}", ""]

    L += [r"\end{document}", "", _footer(doc, idx, cfg, words)]
    return "\n".join(L) + "\n"


def _progression_line(role: dict) -> str:
    """The template wants progression as a bullet, not a subtitle -- subtitle
    form confused parsers -- and it wants it condensed.

    facts.yaml stores the full ladder ("A, then B, then C, then D, then E").
    Printed whole that is two lines, and two lines is most of what pushes these
    documents onto a second page. First rung to last is the same claim in one
    line: no counting, no arithmetic, nothing that is not in the string.
    """
    rungs = [r.strip() for r in re.split(r",?\s*\bthen\b\s*", role["progression"]) if r.strip()]
    if len(rungs) < 2:
        return f"Progressed through {role['progression']}."
    return f"Progressed from {rungs[0]} to {rungs[-1]}."


# The template prints four certifications and comments the rest out. Every line
# here is one line closer to a second page, and the checklist wants one.
MAX_CERTS = 4


def _certifications(cfg: dict, jd: str) -> tuple[list[dict], list[dict]]:
    """Honour each entry's `use_when`. AZ-104 first; filler last and only if room.

    Returns (printed, held_back). Nothing is discarded -- what does not fit is
    listed as a comment in the .tex so it can be swapped in by hand.
    """
    out, filler = [], []
    for c in cfg.get("certifications") or []:
        if not c.get("verified", True):
            continue
        when = str(c.get("use_when") or "")
        if ("AI" in when or "GenAI" in when) and not any(t in jd.lower() for t in _AI_TERMS):
            continue
        (filler if "filler" in when.lower() else out).append(c)
    ordered = out + filler
    return ordered[:MAX_CERTS], ordered[MAX_CERTS:]


def _footer(doc: dict, idx: dict, cfg: dict, words: int) -> str:
    """Provenance + the template's pre-send checklist, as LaTeX comments.

    The .md carries the same provenance for reading; this copy is here so the
    file you are editing can be audited without opening a second one.
    """
    job = doc["job"]
    L = ["% " + "=" * 74, "%  PROVENANCE -- every bullet above, and the fact it came from",
         "% " + "=" * 74]
    for b in doc.get("bullets", []):
        entry = idx.get(b["id"])
        L.append(f"%  {b['id']}  ({entry['group'].get('name') or entry['group'].get('company')})"
                 if entry else f"%  {b['id']}")
        L.append(comment(entry["fact"]["text"] if entry else "(not found in facts.yaml)"))
    flags = doc.get("flags") or {}
    if flags.get("dropped"):
        L += ["%", "%  DROPPED by the never_claim gate (facts.yaml):"]
        for d in flags["dropped"]:
            L.append(comment(f"{d['id']}: {d['reason']}"))
    if flags.get("drift"):
        L += ["%", "%  DRIFT -- these bullets state a technology or a NUMBER their fact does not.",
              "%  Not forbidden, but read them against the provenance above before sending:"]
        for d in flags["drift"]:
            bits = list(d.get("terms") or []) + [f"the number {n}" for n in d.get("numbers") or []]
            L.append(comment(f"{d['id']} introduced: {', '.join(bits)}"))
    # RESUME_PLAYBOOK.md 13 marks these "[auto]". Answer them rather than
    # leaving a box to eyeball -- the same move as reading the page count out of
    # the engine's log instead of counting pages by hand.
    printed = [doc.get("summary", "")] + [b.get("text", "") for b in doc.get("bullets", [])]
    offenders = [b["id"] for b in doc.get("bullets", []) if pronouns_in(b.get("text", ""))]
    if pronouns_in(doc.get("summary", "")):
        offenders.insert(0, "summary")
    role_ids = {f["id"] for section in ("roles",) for g in (cfg.get(section) or [])
                for f in (g.get("facts") or [])}
    experience = [b.get("text", "") for b in doc.get("bullets", []) if b["id"] in role_ids]
    teamwork = [b["id"] for b in doc.get("bullets", [])
                if b["id"] in role_ids and _TEAMWORK.search(b.get("text", ""))]

    L += ["%", "% " + "=" * 74, "%  AUTOMATED CHECKS", "% " + "=" * 74]
    L.append("%  pronouns:  " + ("none" if not offenders
                                 else f"FOUND in {', '.join(offenders)} -- 7a forbids I/my/me"))
    if not experience:
        L.append("%  teamwork:  no Experience bullets selected, nothing to check")
    else:
        if teamwork:
            L.append(f"%  teamwork:  present ({', '.join(teamwork)})")
        else:
            # Name the fact IDs that would fix it, read out of facts.yaml rather
            # than hardcoded -- the mechanism belongs in this public repo, the
            # work history does not.
            candidates = [f["id"] for g in (cfg.get("roles") or [])
                          for f in (g.get("facts") or [])
                          if f.get("verified") and _TEAMWORK.search(f.get("text", ""))]
            hint = (f" Facts that would satisfy it: {', '.join(candidates)}."
                    if candidates else "")
            L.append("%  teamwork:  MISSING -- one collaboration signal belongs in "
                     "Experience." + hint)
    L.append(f"%  summary:   {words} words (7a asks for 70-90)")

    L += ["%", "% " + "=" * 74, "%  PRE-SEND CHECKLIST", "% " + "=" * 74,
          "%  CONTENT",
          "%  [ ] Every bullet traces to a fact ID above (the generator enforces this)",
          "%  [ ] No claim from facts.yaml never_claim appears anywhere",
          "%  [ ] No metric that is not in the fact text it came from",
          "%  [ ] On-call written as L1/L2, never L2/L3",
          "%  [ ] Magento never called idempotent or production-grade",
          "%  [ ] 97 percent claims the measurement, not the architecture as its cause (B002)",
          "%  [ ] Magento repo URL omitted (blocker B001 unresolved)",
          "%  FORMATTING",
          f"%  [ ] Summary is 70-90 words -- currently {words}",
          "%  [ ] No personal pronouns -- see AUTOMATED CHECKS above",
          "%  [ ] At least one teamwork signal in Experience -- see above",
          "%  [ ] No 'personal projects' label inside any skill row",
          "%  [ ] No visible gap disclaimers in the body; max ONE bridge phrase",
          "%  [ ] No arrow, greater-than or pipe inside skill or bullet content",
          "%  [ ] No personal pronouns (I, my, me)",
          "%  [ ] Fits on ONE page (two only if the role genuinely warrants it)",
          "%  MECHANICS",
          "%  [ ] Compiles; braces balanced",
          "%  [ ] Exported to PDF before sending -- never send .tex",
          "% " + "=" * 74, "%  COVER NOTE (not part of the resume -- paste where the form asks)",
          "% " + "=" * 74, comment(doc.get("cover_note", ""), "%  "),
          "%", "%  BE READY FOR THIS QUESTION:", comment(doc.get("gap_honesty", ""), "%  "),
          "%", f"%  Apply: {job.get('apply_url') or job.get('url') or ''}"]
    return "\n".join(L)
