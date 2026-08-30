"""7.37 -- the .tex generator.

The template is not a layout. It carries rules: a tagline that is a role
descriptor and not the job title, skill rows reordered to the JD, no "personal
projects" caveat in a skill row, the Magento repo URL suppressed while blocker
B001 is open, and no arrow/pipe/greater-than in skill or bullet content. A
generator that fills macros and honours none of that produces a document that
compiles and is still wrong.

Escaping is the other half. Tailored text will eventually contain %, &, _, #
and $ -- each either breaks the compile or silently eats the rest of a line.
"""
import re

import pytest

from jobpipe import latexdoc
from jobpipe.latexdoc import ats_clean, escape, render, skill_rows, tex
from jobpipe.render import parse_markdown

CFG = {
    "meta": {"name": "Test User", "location": "Bangalore, India",
             "phone": "+91-00000-00000", "email": "t@example.invalid",
             "portfolio": "https://example.invalid", "github": "https://example.invalid/gh"},
    "skills": {"strong": ["Linux (RHEL/CentOS)", "Windows Server", "Bash"],
               "working": ["Terraform", "Docker", "GitHub Actions"],
               "exposure": ["Kubernetes", "MySQL", "Varnish"]},
    "roles": [{"id": "acme", "company": "Acme Corp", "title": "System Administrator",
               "location": "Bangalore", "start": "2022-09", "end": "present",
               "progression": "Trainee, then Engineer, then Administrator",
               "facts": [
                   {"id": "F001", "verified": True, "tags": ["linux"],
                    "text": "Administered RHEL and CentOS server fleets"},
                   {"id": "F013", "verified": True, "tags": ["award"],
                    "text": "Received the Key Contributor Recognition"}]}],
    "projects": [
        {"id": "cp", "name": "CloudPulse", "year": "2025",
         "url": "https://example.invalid/cp", "stack": ["Go 1.25", "Terraform", "Docker"],
         "facts": [{"id": "P001", "verified": True, "tags": ["iac"],
                    "text": "Provisioned the environment with Terraform"}]},
        {"id": "mg", "name": "Magento_DeployKit", "year": "2025", "url": "",
         "stack": ["Bash", "NGINX", "MySQL 8"],
         "facts": [{"id": "P030", "verified": True, "tags": ["bash"],
                    "text": "Automated the bring-up of a nine-service stack"}]}],
    "education": [{"id": "E001", "verified": True, "degree": "B.Tech",
                   "institution": "Test Institute", "year": "2020",
                   "coursework": ["Operating Systems", "Computer Networks", "Databases"]}],
    "certifications": [
        {"id": "C001", "verified": True, "name": "AZ-104", "status": "complete"},
        {"id": "C004", "verified": True, "name": "GenAI Mastermind", "status": "complete",
         "use_when": "only if the JD mentions AI, GenAI, LLMs or AIOps"}],
    "never_claim": ["Jenkins - never used",
                    "Kubernetes in production - CKA planned, not yet hands-on"],
}


def _doc(description="Linux Terraform Docker administration", title="DevOps Engineer", **kw):
    doc = {"job": {"id": 1, "title": title, "company": "Target Inc", "location": "Bangalore",
                   "description": description, "score": 80, "score_reason": "good",
                   "apply_url": "https://example.invalid/apply", "url": ""},
           "summary": "A summary.", "cover_note": "A note.", "gap_honesty": "A gap.",
           "bullets": [{"id": "F001", "text": "Administered RHEL and CentOS fleets",
                        "original": "Administered RHEL and CentOS server fleets"},
                       {"id": "P001", "text": "Provisioned with Terraform",
                        "original": "Provisioned the environment with Terraform"}],
           "flags": {}}
    doc.update(kw)
    return doc


# --------------------------------------------------------------------------
# escaping
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("100%", r"100\%"), ("A&B", r"A\&B"), ("a_b", r"a\_b"), ("#1", r"\#1"),
    ("$5", r"\$5"), ("{x}", r"\{x\}"), ("a~b", r"a\textasciitilde{}b"),
    ("2^3", r"2\textasciicircum{}3"),
])
def test_every_latex_special_character_is_escaped(raw, expected):
    assert escape(raw) == expected


def test_a_backslash_does_not_re_escape_its_own_replacement():
    r"""The classic .replace() chain bug: substitute \ last and \% becomes
    \textbackslash{}\%."""
    assert escape(r"\%") == r"\textbackslash{}\%"


def test_a_unicode_substitution_that_emits_latex_is_not_escaped_again():
    """The same bug in the other direction -- if the times sign is replaced
    before escaping, its own $ and backslash get escaped."""
    assert escape("3×4") == r"3$\times$4"
    assert escape("a—b") == "a---b"


def test_characters_pdflatex_cannot_typeset_are_dropped_not_passed_through():
    """"Unicode character not set up for use with LaTeX" kills the compile, and
    a job description will eventually contain one."""
    out = escape("café 日本語 🚀 ok")
    assert "café" in out and "ok" in out
    assert "日" not in out and "🚀" not in out


@pytest.mark.parametrize("raw", ["a -> b", "a → b", "a > b", "a | b"])
def test_the_separators_the_template_forbids_become_commas(raw):
    """"Scanners flag mixed bullet symbols." $|$ survives only in the macros
    that build it themselves."""
    cleaned = ats_clean(raw)
    assert not re.search(r"->|→|(?<=\s)[>|](?=\s)", cleaned), cleaned
    assert "," in cleaned


def test_escaping_survives_a_bullet_that_contains_everything_at_once():
    out = tex("Cut cost 40% via C&D_2 {prod} -> $0 spend #1")
    for ch, ok in (("%", r"\%"), ("&", r"\&"), ("_", r"\_"), ("$", r"\$"), ("#", r"\#")):
        assert ok in out
    assert "->" not in out


# --------------------------------------------------------------------------
# the document
# --------------------------------------------------------------------------
def test_the_document_is_structurally_complete_and_braces_balance():
    out = render(_doc(), CFG)
    assert out.count(r"\begin{document}") == 1 and out.count(r"\end{document}") == 1
    body = out.split(r"\end{document}")[0]
    # Comment lines carry prose that may hold a stray brace; the body must not.
    code = "\n".join(l for l in body.splitlines() if not l.lstrip().startswith("%"))
    depth = 0
    for i, ch in enumerate(code):
        if ch in "{}" and (i == 0 or code[i - 1] != "\\"):
            depth += 1 if ch == "{" else -1
        assert depth >= 0, "closed a brace that was never opened"
    assert depth == 0, f"{depth} unclosed brace(s)"


def test_every_itemize_is_closed_and_none_is_empty():
    """An empty itemize is a LaTeX error, and a role with no selected bullets
    is the way to produce one."""
    out = render(_doc(), CFG)
    assert out.count(r"\begin{itemize}") == out.count(r"\end{itemize}")
    for block in re.findall(r"\\begin\{itemize\}(.*?)\\end\{itemize\}", out, re.S):
        assert r"\item" in block


def test_the_job_header_keeps_a_space_before_its_divider():
    """The template writes the divider as \textcolor{lightgrey}{ $|$ #2}. TeX
    skips spaces while scanning an undelimited macro argument, so that space is
    eaten and the line reads "Technology Operations| Employer". The space has
    to sit outside the argument."""
    out = render(_doc(), CFG)
    macro = next(l for l in out.splitlines() if l.startswith(r"\newcommand{\jobheader}"))
    assert r"#1}\ \textcolor{lightgrey}{$|$ #2}" in macro, macro


def test_the_tagline_is_a_role_descriptor_not_the_job_title():
    """Template rule: "GOOD: DevOps Engineer | AWS ... BAD: Senior
    Administrator - DevOps (DV2)"."""
    out = render(_doc(title="Senior Administrator - DevOps (DV2), Requisition 41552"), CFG)
    tagline = re.search(r"\\textit\{\\color\{lightgrey\} (.+?) \$\|\$", out).group(1)
    assert tagline == "DevOps Engineer"
    printed = "\n".join(l for l in out.splitlines() if not l.lstrip().startswith("%"))
    assert "DV2" not in printed and "Requisition" not in printed


def test_skill_rows_are_ordered_by_what_the_jd_asks_for():
    out = render(_doc(description="terraform terraform terraform infrastructure as code"), CFG)
    rows = re.findall(r"\\skillrow\{([^}]+)\}", out)
    assert rows[0] == "Infrastructure as Code", rows


# Enough skills to force more rows than the cap, so the fold actually runs.
_WIDE = {**CFG, "skills": {
    "strong": ["Linux (RHEL/CentOS)", "Windows Server", "Bash", "PowerShell", "Git"],
    "working": ["Azure", "AWS", "Terraform", "Docker", "Docker Compose",
                "GitHub Actions", "NGINX", "Python", "Go"],
    "exposure": ["Kubernetes", "MySQL", "Varnish", "VMware"]}}


# --------------------------------------------------------------------------
# skill rows come from the master resume's own inventory
# --------------------------------------------------------------------------
_ROWS = {**CFG, "skill_rows": [
    {"label": "Containers", "match": ["docker", "kubernetes"],
     "text": "Docker, multi-stage builds, image size optimisation"},
    {"label": "Networking", "match": ["dns", "tcp/ip", "vpn"],
     "text": "TCP/IP, DNS, SSH, VPN, load balancers, firewalls"},
    {"label": "AI-Assisted Engineering", "match": ["ai", "llm"],
     "use_when": "only if the JD mentions AI, GenAI, LLMs or AIOps",
     "text": "Generative AI certification; applies AI assistants with cross-checking"},
]}


def test_skill_rows_use_the_master_inventory_when_facts_yaml_carries_one():
    """The generator used to synthesise "Infrastructure as Code: Terraform" from
    a bare tool list. The master writes a sentence naming ten more searchable
    terms, and an ATS reads those two very differently."""
    rows = dict(skill_rows("docker kubernetes containers", _ROWS)[0])
    assert rows["Containers"] == "Docker, multi-stage builds, image size optimisation"


def test_prose_skill_rows_are_ordered_by_what_the_jd_asks_for():
    labels = [l for l, _ in skill_rows("dns vpn tcp/ip networking", _ROWS)[0]]
    assert labels[0] == "Networking", labels


def test_a_gated_skill_row_appears_only_when_the_jd_matches():
    assert "AI-Assisted Engineering" not in dict(skill_rows("docker linux", _ROWS)[0])
    assert "AI-Assisted Engineering" in dict(skill_rows("AIOps platform work", _ROWS)[0])


def test_a_prose_row_reaches_the_tex_intact():
    doc = _doc(description="docker kubernetes")
    out = render(doc, _ROWS)
    assert r"\skillrow{Containers}{Docker, multi-stage builds, image size optimisation}" in out


def test_a_row_holding_a_strong_skill_is_never_folded_away():
    """Measured on a real DevOps posting: the JD never spelled out "Bash", so the
    scripting row ranked last and got folded under a heading called "Also" --
    burying Bash, PowerShell, Python and Go, which is the candidate's strongest
    ground. A JD not naming a skill is not the same as the skill not mattering."""
    jd = " ".join(["terraform docker kubernetes azure aws nginx"] * 4)
    rows = dict(skill_rows(jd, _WIDE)[0])
    assert set(rows.get("Scripting and Languages", [])) >= {"Bash", "PowerShell"}
    folded = set(rows.get("Also", []))
    assert not ({"Bash", "PowerShell", "Git", "Linux (RHEL/CentOS)",
                 "Windows Server"} & folded), f"a strong skill was folded away: {folded}"


def test_folding_that_saves_no_line_is_not_done():
    """An "Also" row built from one original row costs the same line and reads
    worse than the heading it replaced."""
    for jd in ("terraform docker azure aws nginx",
               "kubernetes mysql varnish vmware terraform azure"):
        rows = dict(skill_rows(jd, _WIDE)[0])
        if "Also" in rows:
            assert len(rows["Also"]) > 1, \
                f"a one-item Also row should have kept its own heading: {rows['Also']}"


def test_an_exposure_skill_appears_only_when_the_jd_names_it():
    """The template's rule is to omit a non-claimable skill silently -- never to
    caveat it. Labelling everything as non-production is what made an earlier
    batch of resumes read as a list of caveats."""
    assert "Kubernetes" not in render(_doc(description="linux terraform"), CFG)
    assert "Kubernetes" in render(_doc(description="linux terraform kubernetes"), CFG)


def test_a_never_claim_technology_stays_out_of_the_tagline():
    """A skill row is a list the user maintains; six words under your name read
    as your core competencies. facts.yaml puts Kubernetes in `exposure` and
    never_claim says "not yet hands-on" -- it can appear in a row, not in a
    headline."""
    out = render(_doc(description="kubernetes kubernetes linux terraform"), CFG)
    header, body = out.split(r"\end{center}", 1)
    assert "Kubernetes" not in header
    assert "Kubernetes" in body, "it should still be listed among the skill rows"


def test_no_skill_row_ever_says_personal_projects():
    rows = re.findall(r"\\skillrow\{[^}]*\}\{([^}]*)\}", render(_doc(), CFG))
    assert rows
    assert not any("personal project" in r.lower() for r in rows)


def test_a_project_with_no_url_in_facts_yaml_prints_no_url():
    """Blocker B001: the Magento repo URL must not be published while live
    credentials remain in its history. facts.yaml carries a blank url; the
    renderer must not invent a link from the project name."""
    doc = _doc()
    doc["bullets"] = [{"id": "P030", "text": "Automated a nine-service stack",
                       "original": "Automated the bring-up of a nine-service stack"}]
    out = render(doc, CFG)
    assert "Magento" in out
    assert not re.search(r"\\href\{[^}]*[Mm]agento", out)


def test_the_genai_certificate_appears_only_when_the_jd_mentions_ai():
    assert "GenAI" not in render(_doc(description="linux terraform docker"), CFG)
    assert "GenAI" in render(_doc(description="linux AIOps platform work"), CFG)


def test_the_progression_is_a_condensed_bullet_not_a_subtitle():
    """Subtitle form confused parsers, so it is a bullet -- and the template
    condenses it. facts.yaml stores the whole ladder; printed in full that is
    two lines, which is most of what pushes a document onto a second page.
    First rung to last is the same claim, and adds no count the string lacks."""
    out = render(_doc(), CFG)
    assert r"\item Progressed from Trainee to Administrator." in out
    assert "then Engineer" not in out


def test_an_award_bullet_sorts_to_the_end_of_its_role():
    doc = _doc()
    doc["bullets"] = [{"id": "F013", "text": "Received the Key Contributor Recognition",
                       "original": "Received the Key Contributor Recognition"},
                      {"id": "F001", "text": "Administered RHEL fleets",
                       "original": "Administered RHEL and CentOS server fleets"}]
    out = render(doc, CFG)
    assert out.index("Administered RHEL fleets") < out.index("Key Contributor")


def test_every_bullet_carries_its_fact_id_and_its_source_text():
    """The .md's provenance line is the gate's second half; the .tex keeps its
    own copy so the file being edited can be audited without opening another."""
    out = render(_doc(), CFG)
    for fid, source in (("F001", "Administered RHEL and CentOS server fleets"),
                        ("P001", "Provisioned the environment with Terraform")):
        assert f"% {fid}" in out
        assert source in out.split(r"\end{document}")[1]


def test_the_flags_from_the_never_claim_gate_reach_the_tex():
    doc = _doc(flags={"dropped": [{"id": "F999", "reason": "introduced Jenkins"}],
                      "drift": [{"id": "F001", "terms": ["Linux"]}]})
    out = render(doc, CFG)
    assert "introduced Jenkins" in out and "F001 introduced: Linux" in out


def test_a_newline_in_model_output_cannot_escape_a_comment_block():
    """A newline inside a LaTeX comment ends it and dumps the rest into the
    document."""
    doc = _doc(cover_note="First line.\n\\section{Injected}\nSecond line.")
    footer = render(doc, CFG).split(r"\end{document}")[1]
    assert all(l.lstrip().startswith("%") or not l.strip() for l in footer.splitlines())


# --------------------------------------------------------------------------
# the checks RESUME_PLAYBOOK.md section 13 marks "[auto]"
# --------------------------------------------------------------------------
def test_a_personal_pronoun_is_reported():
    """7a: "No personal pronouns. Never I, my, me." Found live in a prepared
    document: "I have hands-on experience deploying AWS..." was about to go out."""
    out = render(_doc(summary="I have hands-on experience deploying AWS."), CFG)
    assert "pronouns:  FOUND in summary" in out
    assert "pronouns:  none" in render(_doc(), CFG)


def test_a_lowercase_i_is_not_mistaken_for_the_pronoun():
    assert "pronouns:  none" in render(_doc(summary="Ran i/o profiling on the fleet."), CFG)


def test_a_missing_teamwork_signal_is_reported_with_the_facts_that_would_fix_it():
    """7a: scanners look for collaboration language. Three of nine prepared
    documents had none."""
    out = render(_doc(), CFG)
    assert "teamwork:  MISSING" in out
    doc = _doc()
    doc["bullets"] = [{"id": "F001", "original": "Administered RHEL and CentOS server fleets",
                       "text": "Coordinated RHEL patching with the application and "
                               "security teams"}]
    assert "teamwork:  present (F001)" in render(doc, CFG)


def test_a_teamwork_signal_in_a_project_bullet_does_not_count():
    """The rule is about Experience -- that is where a scanner looks for evidence
    of working with other people. A solo side project collaborating with nobody
    must not satisfy it."""
    doc = _doc()
    doc["bullets"] = [
        {"id": "F001", "original": "Administered RHEL and CentOS server fleets",
         "text": "Administered RHEL and CentOS fleets"},
        {"id": "P001", "original": "Provisioned the environment with Terraform",
         "text": "Collaborated with stakeholders on Terraform provisioning"}]
    assert "teamwork:  MISSING" in render(doc, CFG)


def test_with_no_experience_bullets_the_teamwork_check_says_so_rather_than_failing():
    """Reporting MISSING when there was nothing to look at would be a wrong
    diagnostic, and a wrong diagnostic is worse than none (CLAUDE.md 7.32)."""
    doc = _doc()
    doc["bullets"] = [{"id": "P001", "original": "Provisioned the environment with Terraform",
                       "text": "Provisioned with Terraform"}]
    assert "teamwork:  no Experience bullets selected" in render(doc, CFG)


def test_an_invented_number_reaches_the_tex():
    doc = _doc(flags={"drift": [{"id": "F007", "terms": [], "numbers": ["7", "24"]}]})
    assert "F007 introduced: the number 7, the number 24" in render(doc, CFG)


def test_the_summary_word_count_is_reported_against_the_templates_band():
    out = render(_doc(), CFG)
    assert re.search(r"%\s+\d+ words", out)
    assert "70-90" in out


# --------------------------------------------------------------------------
# regenerating from a prepared document
# --------------------------------------------------------------------------
def test_a_prepared_markdown_round_trips_back_into_a_payload():
    """`cli tex` rebuilds from the .md when no sidecar exists. `prepare` costs 2
    of the tailor model's 20 daily calls per job (7.33); regeneration must be
    free."""
    md = ("# DevOps Engineer\n**Target Inc** - Bangalore\n"
          "Fit score: **80** - good match\n\nApply: https://example.invalid/a\n\n---\n\n"
          "## Summary\nA summary.\n\n## Tailored bullets\n\n"
          "- Administered RHEL and CentOS fleets\n"
          "  <sub>from F001: Administered RHEL and CentOS server fleets</sub>\n"
          "- Provisioned the environment with Terraform\n\n"
          "## Cover note\n\nA note.\n\n## Be ready for this question\n\nA gap.\n")
    doc = parse_markdown(md, None, CFG)
    assert doc["job"]["title"] == "DevOps Engineer"
    assert doc["job"]["company"] == "Target Inc" and doc["job"]["score"] == 80
    assert doc["summary"] == "A summary." and doc["cover_note"] == "A note."
    assert [b["id"] for b in doc["bullets"]] == ["F001", "P001"], \
        "an unannotated bullet must be matched back to facts.yaml by its text"
    assert doc["bullets"][0]["original"] == "Administered RHEL and CentOS server fleets"


def test_an_unsourceable_bullet_is_reported_rather_than_silently_dropped():
    md = ("# T\n**C** - L\n\n## Tailored bullets\n\n- Something nobody wrote down\n")
    doc = parse_markdown(md, None, CFG)
    assert doc["bullets"] == []
    assert doc["flags"]["unsourced"] == ["Something nobody wrote down"]


def test_a_skill_no_row_claims_still_surfaces():
    """A skill the user adds that no SKILL_ROWS entry knows about must not
    vanish -- it has to appear somewhere and the generator has to say so.

    Asserted against a fixture, never against the real config/facts.yaml. CI
    runs `make config`, which generates the config from the *example* template,
    so a test that reads the user's private file passes locally and fails in CI
    for reasons that have nothing to do with the code. tests/test_facts_gate.py
    already learned this once."""
    cfg = {**CFG, "skills": {"strong": ["Bash"], "working": [],
                             "exposure": ["Nonesuch Framework"]}}
    rows, leftover = skill_rows("nonesuch framework bash", cfg)
    body = " ".join(items if isinstance(items, str) else " ".join(items)
                    for _, items in rows) + " " + " ".join(leftover)
    assert "Nonesuch Framework" in body, "an unclaimed skill disappeared"

    out = render(_doc(description="nonesuch framework bash"), cfg)
    assert "Nonesuch Framework" in out
    assert "not in latexdoc.SKILL_ROWS" in out, "the generator should flag it"
