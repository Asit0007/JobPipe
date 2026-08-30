"""7.36 -- the half of the facts gate that catches drift rather than invention.

facts.yaml has carried 20 never_claim rules since the project started and
nothing read them. The ID gate cannot help here: it validates that a returned
fact ID is real and verified, not that the sentence built from it still says
what the fact said. The one real drift this project has observed (CLAUDE.md
section 9) passed the ID gate cleanly -- F012 "Windows Server 2016-2022" came
back as "Windows AND LINUX servers".

The rules are prose, so extraction is a heuristic. These tests pin the two
things that must not regress: the terms that have to be found, and the terms
that must NOT be, because a false drop deletes a good bullet silently.
"""
import pytest

from jobpipe.claims import audit, never_claim_terms, numbers, tech_vocab
from jobpipe.render import gate

CFG = {
    "meta": {"name": "Test User"},
    "skills": {
        "strong": ["Linux (RHEL/CentOS)", "Windows Server", "Bash"],
        "working": ["Azure", "Terraform", "Docker", "Python"],
        "exposure": ["Kubernetes", "MySQL", "VMware", "HashiCorp Vault"],
    },
    "roles": [{
        "id": "acme", "company": "Acme Corp", "title": "Admin",
        "location": "Bangalore", "start": "2022-09", "end": "present",
        "facts": [
            {"id": "F012", "verified": True, "tags": ["windows"],
             "text": "Administered Windows Server 2016 through 2022 including "
                     "provisioning, hardening and performance tuning"},
            {"id": "F007", "verified": True, "tags": ["oncall"],
             "text": "Served as L1/L2 on-call engineer handling production incident response"},
        ],
    }],
    "projects": [
        {"id": "cp", "name": "CloudPulse", "year": "2025",
         "url": "https://example.invalid/cp", "stack": ["Go", "Terraform", "Docker"],
         "facts": [{"id": "P003", "verified": True, "tags": ["cicd"],
                    "text": "Built a GitHub Actions pipeline that pushes to Docker Hub"}]},
        {"id": "mg", "name": "Magento_DeployKit", "year": "2025", "url": "",
         "stack": ["Bash", "NGINX", "MySQL 8", "Redis"],
         "facts": [{"id": "P030", "verified": True, "tags": ["bash"],
                    "text": "Automated the bring-up of a nine-service stack "
                            "(PHP-FPM, MySQL, Redis, NGINX) on a single host"}]},
    ],
    "never_claim": [
        "Jenkins, GitLab CI, GCP - never used",
        "New Relic, Loki, Dynatrace - observability is CloudWatch only",
        "SQL Server, SSMS - database work is self-managed MySQL 8",
        "Exchange Online, SharePoint - Microsoft 365 administration never done",
        "Kubernetes in production - CKA planned, not yet hands-on",
        "Python at Acme Corp - project and coursework only",
        "Redis or MySQL in QuantBot - it uses flat files, no database at all",
        "Prometheus or Grafana ANYWHERE - never implemented in any project",
        "CloudPulse on ECS Fargate or ECR - it runs on EC2 with images from Docker Hub",
        "Azure AD / Entra ID / RBAC operational administration - SME-held",
        "VMware provisioning, snapshots, templates, golden images - tutorial familiarity",
        "Magento_DeployKit as idempotent, production-grade, or a working one-shot install",
        "CloudPulse Vault as Terraform-provisioned - it is manual out-of-band SSH setup",
    ],
}


def _terms():
    return {t for t, _ in never_claim_terms(CFG)}


# --------------------------------------------------------------------------
# extraction: what the gate looks for
# --------------------------------------------------------------------------
@pytest.mark.parametrize("term", [
    "Jenkins", "GitLab CI", "GCP", "Prometheus", "Grafana", "Kubernetes",
    "Python", "Redis", "MySQL", "ECS Fargate", "ECR", "Entra ID", "snapshots",
    "idempotent", "production-grade",
])
def test_a_forbidden_term_is_extracted(term):
    assert term in _terms(), f"{term!r} is in a never_claim rule but the gate cannot see it"


@pytest.mark.parametrize("name", [
    "CloudPulse",          # "CloudPulse on ECS Fargate" forbids Fargate, not the project
    "Magento_DeployKit",   # same shape
    "Azure",               # "Azure AD" forbids Entra admin, not the cloud the user works in
    "VMware",              # "VMware provisioning" forbids the activity, not the platform
    "Acme Corp",           # the employer's own name
])
def test_a_name_the_user_owns_is_never_a_forbidden_term(name):
    """The dangerous failure mode. A fact's text never repeats its project name,
    so treating "CloudPulse" as forbidden deletes every CloudPulse bullet."""
    assert name not in _terms()


def test_the_drift_vocabulary_excludes_whatever_is_already_forbidden():
    assert not (_terms() & set(tech_vocab(CFG))), "a term cannot be both a drop and a flag"


def test_every_rule_yields_at_least_one_term():
    """A rule that expands to nothing is a rule that is silently not enforced."""
    covered = {rule for _, rule in never_claim_terms(CFG)}
    assert covered == set(CFG["never_claim"])


# --------------------------------------------------------------------------
# matching: drift, not substring
# --------------------------------------------------------------------------
def test_an_invented_forbidden_claim_is_dropped():
    kept, flags = gate([{"id": "P003", "text": "Built Jenkins pipelines and Prometheus "
                                               "dashboards", "original": "x"}], {}, CFG)
    assert kept == []
    assert flags["dropped"][0]["id"] == "P003"
    assert "Jenkins" in flags["dropped"][0]["reason"]


def test_a_forbidden_term_the_source_fact_already_states_is_allowed():
    """Redis is forbidden as a QuantBot claim and appears legitimately in the
    Magento fact. Comparing against the source fact tells them apart with no
    special case -- that is the whole design."""
    kept, flags = gate([{"id": "P030", "original": "x",
                         "text": "Automated a nine-service stack including Redis and MySQL"}],
                       {}, CFG)
    assert len(kept) == 1 and not flags.get("dropped")


def test_the_f012_drift_from_the_ledger_is_caught():
    """CLAUDE.md section 9, verbatim. This passed the ID gate and shipped."""
    kept, flags = gate([{
        "id": "F012",
        "original": CFG["roles"][0]["facts"][0]["text"],
        "text": "Managed system services, user file permissions and package management "
                "tools across Windows and Linux servers",
    }], {}, CFG)
    assert flags["drift"][0]["terms"] == ["Linux"], "the added technology went unnoticed"
    assert len(kept) == 1, "drift is a flag, not a drop -- there is no forbidden claim here"


def test_a_faithful_rewrite_raises_nothing():
    kept, flags = gate([{
        "id": "F012",
        "original": CFG["roles"][0]["facts"][0]["text"],
        "text": "Administered Windows Server 2016 through 2022, covering provisioning, "
                "hardening and performance tuning",
    }], {}, CFG)
    assert len(kept) == 1 and flags == {}


def test_a_projects_own_stack_is_not_drift():
    """A CloudPulse bullet may say Docker: Docker is in CloudPulse's stack."""
    _, flags = gate([{"id": "P003", "original": "x",
                      "text": "Built a Docker and Terraform deployment pipeline"}], {}, CFG)
    assert not flags.get("drift")


def test_the_employer_being_applied_to_may_be_named_in_the_cover_note():
    """Measured regression: the only false positive across nine real prepared
    documents was "GitLab" in a cover note addressed to GitLab. The rule
    forbids GitLab CI."""
    out = {"job": {"company": "GitLab", "title": "Support Engineer"},
           "cover_note": "Supporting GitLab's customer base requires Linux administration."}
    _, flags = gate([{"id": "F007", "original": CFG["roles"][0]["facts"][1]["text"],
                      "text": CFG["roles"][0]["facts"][1]["text"]}], out, CFG)
    assert not flags.get("prose")


def test_a_forbidden_claim_in_prose_is_flagged_but_not_deleted():
    """Dropping the summary empties a required section, and prose has no single
    source fact to check against, so it is likelier to be a false positive."""
    out = {"job": {"company": "Acme"},
           "summary": "Built Jenkins pipelines across the estate."}
    kept, flags = gate([{"id": "F007", "original": CFG["roles"][0]["facts"][1]["text"],
                         "text": CFG["roles"][0]["facts"][1]["text"]}], out, CFG)
    assert flags["prose"][0]["field"] == "summary"
    assert "Jenkins" in flags["prose"][0]["terms"]
    assert len(kept) == 1


@pytest.mark.parametrize("word", ["New", "SQL", "Exchange"])
def test_an_ordinary_word_that_opens_a_product_name_is_not_a_term(word):
    """"New Relic" must not contribute "New" -- it would fire on "a new pipeline".
    "Exchange Online" must not contribute "Exchange" -- QuantBot talks to a crypto
    exchange. "SQL Server" must not contribute "SQL" -- that is what you write
    against MySQL. The full two-word term still matches, which is the product."""
    assert word not in _terms()
    assert any(t.startswith(word + " ") for t in _terms()), \
        f"the full product name starting {word!r} should still be a term"


# --------------------------------------------------------------------------
# invented numbers -- RESUME_PLAYBOOK.md section 5
# --------------------------------------------------------------------------
def test_a_number_the_source_fact_does_not_state_is_flagged():
    """Measured on the nine prepared documents: 1 of 69 bullets tripped this, and
    it was real. An "L1/L2 on-call engineer" fact came back as "24/7 on-call
    support" -- a shift pattern nobody wrote down and an interviewer can ask about."""
    _, flags = gate([{"id": "F007", "original": CFG["roles"][0]["facts"][1]["text"],
                      "text": "Provided 24/7 on-call support as an L1/L2 incident "
                              "response engineer"}], {}, CFG)
    assert flags["drift"][0]["numbers"] == ["7", "24"]


def test_a_number_the_source_fact_does_state_is_not_flagged():
    _, flags = gate([{"id": "F012", "original": CFG["roles"][0]["facts"][0]["text"],
                      "text": "Administered Windows Server 2016 through 2022"}], {}, CFG)
    assert not flags.get("drift")


def test_thousands_separators_and_plus_signs_compare_equal():
    src = "Ran monthly patch cycles across 2000+ Windows and Linux servers"
    assert numbers("patched 2,000 servers") <= numbers(src)


def test_a_spelled_out_numeral_matches_its_digit():
    """"five consecutive losses" and "5 consecutive losses" are the same claim."""
    assert numbers("a 48 hour pause after 5 consecutive losses") <= \
        numbers("Chose a flat forty-eight hour circuit breaker after five consecutive losses"
                " and 48 hours of cooldown")


def test_a_decimal_and_its_integer_are_the_same_quantity():
    assert numbers("3 MB") <= numbers("page weight of 3.0 MB")


def test_the_gate_accepts_the_live_sqlite_row_prepare_actually_hands_it():
    """`prepare` passes the DB row straight through. sqlite3.Row supports [] but
    not .get(), so a dict-shaped assumption passes every test and dies on the
    only path that matters. It did, on 2026-08-30."""
    import sqlite3
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT 'GitLab' AS company, 'Support Engineer' AS title").fetchone()
    out = {"job": row, "cover_note": "Supporting GitLab's customers.", "summary": ""}
    kept, flags = gate([{"id": "F007", "original": CFG["roles"][0]["facts"][1]["text"],
                         "text": CFG["roles"][0]["facts"][1]["text"]}], out, CFG)
    assert len(kept) == 1 and not flags.get("prose")


def test_a_gap_statement_may_name_the_technology_it_lacks():
    """`gap_honesty` exists to say what the candidate does not have. Flagging
    "the primary gap is direct production management of Kubernetes and EKS" is
    the check working backwards -- and would push the model toward vaguer, less
    honest gaps. Measured: 2 of 4 prose flags in the first real batch were this."""
    out = {"job": {"company": "Contify"}, "summary": "", "cover_note": "",
           "gap_honesty": "The primary gap is direct production management of "
                          "Kubernetes clusters and Jenkins pipelines."}
    _, flags = gate([{"id": "F007", "original": CFG["roles"][0]["facts"][1]["text"],
                      "text": CFG["roles"][0]["facts"][1]["text"]}], out, CFG)
    assert not flags.get("prose")


def test_a_bare_participle_is_not_a_forbidden_term():
    """"CloudPulse Vault as Terraform-provisioned" forbids that Vault is IaC. The
    tail alone forbids nothing -- and it fired on "Terraform-provisioned AWS
    environments", which is true and is a verified fact. The head term
    "CloudPulse Vault" already carries the rule."""
    terms = _terms()
    assert "Terraform-provisioned" not in terms
    assert "CloudPulse Vault" in terms
    # A tail that is not a participle still counts.
    assert "idempotent" in terms


def test_a_substring_inside_a_longer_word_is_not_a_hit():
    assert not audit("Managed GitHub Actions workflows", "", CFG)["forbidden"], \
        "'Git' must not match inside 'GitHub'"
