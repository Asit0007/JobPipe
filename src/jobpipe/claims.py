"""The missing half of the facts gate: never_claim.

`tailor.py`'s ID gate stops the model INVENTING a fact. It structurally cannot
stop the model rephrasing a real fact into a claim the user cannot defend --
which is the only drift this project has actually observed: F012 "Windows
Server 2016-2022" came back as "Windows **and Linux** servers".

facts.yaml has carried 20 `never_claim` rules and 7 blockers since the project
started and nothing read them. This module reads them.

Two checks, deliberately different in severity:

    never_claim   a FORBIDDEN term the rewrite introduced   -> drop the bullet
    drift         any other technology it introduced        -> flag it, keep it

Both are DRIFT checks, not substring checks. A term counts only if the rewrite
contains it and the source fact does not. That is what makes them precise
without a single special case: "Redis" is forbidden as a QuantBot claim yet
appears legitimately in the Magento fact text, and comparing against the source
fact tells those two apart on its own. It is also what catches the F012 drift,
which no forbidden-word list would have: "Linux" is a perfectly good skill, and
the problem was that it appeared in a bullet about Windows.

The rules are prose written by a human, so term extraction is a heuristic and
will never be perfect. `python -m jobpipe.cli claims` prints exactly what each
rule expands to -- audit it, and reword a rule in facts.yaml if the gate is
looking for the wrong thing.
"""
from __future__ import annotations

import re
from functools import lru_cache

_CLAIM_HALF = re.compile(r"\s+[-–—]\s+")          # "<claim> - <why>"
_SPLIT = re.compile(r",|\s+or\s+|\s+and\s+|/", re.I)
# "in" and "at" are locative: whatever follows is where a claim is forbidden,
# never the forbidden thing itself. "on" and "as" go both ways -- "Ansible on
# any project" scopes a claim, "CloudPulse on ECS Fargate" names one -- so
# those tails are read before deciding.
_PREP = re.compile(r"\s+(?:in|at|on|as)\s+", re.I)
_LOCATIVE = re.compile(r"\s+(?:in|at)\s+", re.I)
_ANYWHERE = re.compile(r"\s+ANYWHERE\b", re.I)
_ARTICLE = re.compile(r"^(?:a|an|the)\s+", re.I)

# A tail like this names the CONTEXT a claim is forbidden in, not the forbidden
# thing: "Kubernetes in production", "Ansible on any project".
_CONTEXT_TAIL = re.compile(
    r"^(?:any|this|production|projects?|coursework|resumes?|anywhere|work|use|"
    r"hands-on)\b", re.I)

# Words that would fire on ordinary infrastructure prose. Dropping them costs a
# little coverage; keeping them would drop good bullets, which is worse.
_TOO_GENERIC = {
    "templates", "images", "admin", "administration", "provisioning",
    "any metric", "this file", "production", "tier", "work", "project",
    "projects", "experience", "familiarity",
}

# Determiners never make a useful head token. Without this, "The 97 percent
# reduction ..." contributes the term "The" and the gate fires on everything.
#
# The second group is ordinary English that happens to open a product name.
# "New Relic" must not contribute "New", "Exchange Online" must not contribute
# "Exchange" -- QuantBot talks to a crypto exchange -- and "SQL Server" must not
# contribute "SQL", which is what you write against MySQL. In every one of these
# the full two-word term still matches, which is the actual product anyway.
# "<Product> <activity>" forbids the activity performed on that product, not the
# product. "ESXi host admin" bans administering ESXi hosts -- it does not ban
# reading ESXi telemetry during incident triage, which is real work. So when a
# phrase ends in one of these, the head token is not emitted on its own; the
# full phrase still is.
_ACTIVITY_TAIL = ("admin", "administration", "provisioning", "management",
                  "ops", "operations", "engineering", "development")

_NOT_A_HEAD = {
    "the", "a", "an", "any", "all", "every", "some", "no",
    "new", "exchange", "control", "sql", "open", "active", "data", "core",
    "key", "service", "services", "server", "online", "base", "cloud", "power",
    "smart", "one", "prime", "red", "blue", "web", "net", "app", "apps",
}


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------
def _termish(term: str) -> bool:
    """Is this fragment specific enough to match on without false positives?"""
    t = term.strip(" .\"'")
    if t.lower() in _TOO_GENERIC:
        return False
    # "L3" is two characters and is exactly the claim to catch, so a short
    # token is allowed when it mixes letters and digits.
    if len(t) < 3:
        return len(t) == 2 and any(c.isdigit() for c in t) and any(c.isalpha() for c in t)
    # A product name almost always carries a capital or a digit. A bare
    # lowercase word earns a place only if it is long enough to be distinctive
    # ("production-grade", "idempotent").
    return any(c.isupper() or c.isdigit() for c in t) or len(t) >= 8


def _normalise_skill(item: str) -> list[str]:
    """"Linux (RHEL/CentOS)" -> ["Linux", "RHEL", "CentOS"]; "Go 1.25" -> ["Go"]."""
    pieces: list[str] = []
    inner = re.findall(r"\(([^)]*)\)", item)
    base = re.sub(r"\([^)]*\)", " ", item)
    for chunk in [base, *inner]:
        for piece in re.split(r"[/,]", chunk):
            piece = re.sub(r"\s+v?\d[\d.]*$", "", piece.strip())   # "Varnish 7" -> "Varnish"
            if len(piece) >= 2 and any(c.isalpha() for c in piece):
                pieces.append(piece)
    return pieces


def _own_names(cfg: dict) -> frozenset[str]:
    """Things the user owns and may always name: skills, projects, employers.

    A rule like "CloudPulse on ECS Fargate" forbids ECS Fargate, not the name
    of the user's own project. Without this the gate deletes every CloudPulse
    bullet, because a fact's text never repeats the project name.
    """
    names: set[str] = set()
    for group in (cfg.get("skills") or {}).values():
        for item in group or []:
            names.update(p.lower() for p in _normalise_skill(str(item)))
    for proj in cfg.get("projects") or []:
        names.add(str(proj.get("name", "")).lower())
    for role in cfg.get("roles") or []:
        company = str(role.get("company", ""))
        names.add(company.lower())
        names.update(w.lower() for w in company.split())
    names.discard("")
    return frozenset(names)


def _emit(text: str, banned: frozenset[str], tokens: frozenset[str],
          suppress_own: bool = False) -> list[str]:
    """One fragment -> the terms worth matching on.

    `suppress_own` is set only when this fragment is the SCOPE of a rule rather
    than its subject -- the "CloudPulse" in "CloudPulse on ECS Fargate". When
    the fragment is the subject ("Kubernetes in production") an own-name is
    exactly what we want to match on, so the suppression must not apply.
    """
    text = _ARTICLE.sub("", text.strip(" .\"'"))
    if not text or (suppress_own and text.lower() in banned):
        return []
    out: list[str] = []
    if _termish(text):
        out.append(text)
    words = text.split()
    if len(words) > 1:
        if words[0].lower() in banned:
            # "CloudPulse least-privilege IAM" -> "least-privilege IAM". The
            # remainder must still look like a product, or "VMware provisioning"
            # would contribute the word "provisioning" to every resume.
            rest = " ".join(words[1:])
            # A one-word remainder that is part of a name the user owns is not
            # a claim: "CloudPulse Vault as Terraform-provisioned" forbids one
            # sentence about Vault, not the word Vault.
            own_word = len(words) == 2 and words[1].lower() in tokens
            if _termish(rest) and not own_word and any(c.isupper() or c.isdigit() for c in rest):
                out.append(rest)
        elif not text.lower().endswith(_ACTIVITY_TAIL):
            head = words[0]
            if (head.lower() not in _NOT_A_HEAD and head.lower() not in banned
                    and any(c.isupper() or c.isdigit() for c in head) and _termish(head)):
                out.append(head)
    return out


def _expand(rule: str, banned: frozenset[str], tokens: frozenset[str]) -> list[str]:
    claim = _ANYWHERE.sub("", _CLAIM_HALF.split(rule, maxsplit=1)[0])
    terms: list[str] = []
    for part in _SPLIT.split(claim):
        part = part.strip(" .\"'")
        if not part:
            continue
        m = _PREP.search(part)
        if not m:
            terms += _emit(part, banned, tokens)
            continue
        head, tail = part[:m.start()], _ARTICLE.sub("", part[m.end():].strip())
        # A context tail ("in production", "at <employer>") names the scope the
        # drift comparison already enforces, so the head is the claim. Anything
        # else is itself the forbidden thing, and the head is only the scope.
        if _LOCATIVE.match(m.group(0)) or _CONTEXT_TAIL.match(tail) or tail.lower() in banned:
            terms += _emit(head, banned, tokens)
        else:
            terms += _emit(head, banned, tokens, suppress_own=True)
            # A one-word participle tail describes the head; alone it means
            # nothing. "CloudPulse Vault as Terraform-provisioned" forbids that
            # Vault is IaC -- it does not forbid the phrase, and the head term
            # "CloudPulse Vault" already carries the rule. Measured: the bare
            # "Terraform-provisioned" fired on "Terraform-provisioned AWS
            # environments", which is true and is fact P001.
            if " " in tail or not tail.lower().endswith("ed"):
                terms += _emit(tail, banned, tokens)
    return list(dict.fromkeys(terms))


def never_claim_terms(cfg: dict) -> list[tuple[str, str]]:
    """[(term, the rule it came from)] for every never_claim entry."""
    banned = _own_names(cfg)
    tokens = frozenset(w for name in banned for w in name.split())
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for rule in cfg.get("never_claim") or []:
        for term in _expand(str(rule), banned, tokens):
            if term.lower() not in seen:
                seen.add(term.lower())
                out.append((term, str(rule)))
    return out


def tech_vocab(cfg: dict) -> list[str]:
    """Every technology name the user has written down, for the softer check.

    Drawn from `skills` and each project's `stack` -- both closed, hand-kept
    lists, so this vocabulary grows only when the user says so. Tags are
    deliberately excluded: they carry words like "security" and "automation"
    that appear in every second bullet.
    """
    raw: list[str] = []
    for group in (cfg.get("skills") or {}).values():
        raw.extend(group or [])
    for proj in cfg.get("projects") or []:
        raw.extend(proj.get("stack") or [])

    forbidden = {t.lower() for t, _ in never_claim_terms(cfg)}
    terms: list[str] = []
    for item in raw:
        for piece in _normalise_skill(str(item)):
            if piece.lower() not in forbidden and piece not in terms:
                terms.append(piece)
    return terms


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------
@lru_cache(maxsize=4096)
def _pattern(term: str) -> re.Pattern:
    # Short terms ("Go", "AWS", "L3") match case-sensitively -- "go" and "aws"
    # are ordinary words. Longer ones do not need the protection.
    flags = 0 if len(term) <= 3 else re.IGNORECASE
    return re.compile(rf"(?<![\w-]){re.escape(term)}(?![\w-])", flags)


def _hits(term: str, text: str) -> bool:
    return bool(_pattern(term).search(text))


# A bare number, with the thousands separators and trailing "+" that resume
# prose attaches to one. Version-like strings ("8.2/8.3", "v2") are handled by
# normalisation rather than excluded, because "2000+" and "2,000" must compare
# equal to the "2000" in the fact.
_NUMBER = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\+?")

# Written-out numerals an LLM swaps for digits and back. "five consecutive
# losses" and "5 consecutive losses" are the same claim; without this the
# rewrite of either looks like an invented metric.
_WORD_NUMBERS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12", "twenty": "20", "thirty": "30",
    "forty": "40", "fifty": "50", "sixty": "60", "hundred": "100",
    "thousand": "1000",
}


def numbers(text: str) -> set[str]:
    """Every quantity a piece of text asserts, normalised for comparison.

    RESUME_PLAYBOOK.md section 5 lists the approved metrics and says "never
    invent others". This is the mechanical half of that rule: a number the
    rewrite states and the source fact does not is an invented metric, which is
    the playbook's failure mode 2 -- inflating the scope of real work.
    """
    found = {m.group(1).replace(",", "").rstrip(".") for m in _NUMBER.finditer(text or "")}
    for word, digits in _WORD_NUMBERS.items():
        if re.search(rf"(?<![\w-]){word}(?![\w-])", text or "", re.IGNORECASE):
            found.add(digits)
    # "3.0" and "3" are the same quantity; store both so either spelling matches.
    for n in list(found):
        if n.endswith(".0"):
            found.add(n[:-2])
        elif "." not in n:
            found.add(n + ".0")
    return found


def audit(claim_text: str, source_text: str, cfg: dict) -> dict:
    """What did this rewrite introduce that its source did not say?

    `source_text` is the fact's own text plus whatever context legitimately
    belongs to it -- the parent project's stack, for instance, since a
    CloudPulse bullet may reasonably say "Docker" when Docker is in the stack.
    """
    forbidden = [
        {"term": term, "rule": rule}
        for term, rule in never_claim_terms(cfg)
        if _hits(term, claim_text) and not _hits(term, source_text)
    ]
    hard = {f["term"].lower() for f in forbidden}
    drifted = [
        term for term in tech_vocab(cfg)
        if term.lower() not in hard and _hits(term, claim_text) and not _hits(term, source_text)
    ]
    # Report the canonical spelling only. The "3" / "3.0" pair exists so either
    # spelling matches the other; printing both would just look like two findings.
    raw = numbers(claim_text) - numbers(source_text)
    invented = sorted((n for n in raw if not (n.endswith(".0") and n[:-2] in raw)),
                      key=_as_float)
    return {"forbidden": forbidden, "drifted": drifted, "invented_numbers": invented}


def _as_float(n: str) -> float:
    try:
        return float(n)
    except ValueError:
        return 0.0
