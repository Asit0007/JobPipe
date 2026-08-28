"""Soft penalties and the staffing flag must read the field that carries signal.

Both bugs here were found on the first live scoring run (2026-08-28) and both
are the CLAUDE.md 3 title_reject trap in mirror image: matching title+description
when only one of the two says anything.

Measured against the 4,654-row corpus before the fix:
  - "salesforce" appeared in 520 descriptions and 7 titles. Its -40 was landing
    on investor lists ("Salesforce Ventures" backs Atlan) and CRM tooling.
  - the staffing detector flagged 708 rows, every one of them from a first-party
    ATS board, on the bare word "recruitment" in an EEO footer or privacy notice.
"""
import pytest

from jobpipe.normalize import looks_like_staffing_firm
from jobpipe.score import apply_penalties


def _job(**kw):
    row = {"title": "", "description": "", "company": "", "source": "greenhouse"}
    row.update(kw)
    return row


# --- soft_penalty is scoped to the title ------------------------------------

def test_investor_named_salesforce_does_not_penalise():
    job = _job(
        title="Senior Security Engineer - Corporate Security",
        description="Backed by GIC, Insight Partners, Meritech and Salesforce Ventures,"
                    " we've earned the trust of AI-forward enterprises.",
    )
    score, applied = apply_penalties(80, job)
    assert score == 80, f"investor boilerplate cost {80 - score} points: {applied}"
    assert applied == []


def test_actual_salesforce_role_is_still_penalised():
    score, applied = apply_penalties(80, _job(title="Salesforce Administrator"))
    assert score == 40
    assert applied == ["-40 (salesforce)"]


def test_shift_terms_still_match_the_description():
    """The one class of term the title never states."""
    job = _job(title="Cloud Support Engineer",
               description="This role operates on a rotational shift roster.")
    score, applied = apply_penalties(80, job)
    assert score == 70
    assert applied == ["-10 (rotational shift)"]


# --- the staffing flag reads the company, not the boilerplate ---------------

@pytest.mark.parametrize("boilerplate", [
    "Recruitment Fraud Alert. Your safety is important to us.",
    "We share your personal information with affiliates involved in your"
    " recruitment process, wherever these are located.",
    "Our staffing decisions are made without regard to protected characteristics.",
])
def test_eeo_and_privacy_boilerplate_is_not_a_staffing_repost(boilerplate):
    assert not looks_like_staffing_firm("Atlan", boilerplate, "greenhouse")


def test_staffing_firm_named_in_the_company_still_flags():
    assert looks_like_staffing_firm("ABC Staffing Solutions", "", "adzuna")
    assert looks_like_staffing_firm("Acme Recruitment Pvt Ltd", "", "gmail_alerts")


def test_client_speak_in_the_description_still_flags():
    """No employer writes this about itself, so the description is the right field."""
    assert looks_like_staffing_firm(
        "Acme Corp", "Our client is a leading MNC seeking a Linux admin.", "adzuna")
    assert looks_like_staffing_firm(
        "Acme Corp", "Role is C2H, on the payroll of our partner.", "gmail_alerts")


def test_ats_sources_are_never_staffing_reposts():
    """A first-party board is one row per requisition. Same rationale as the
    fuzzy-dedup carve-out in CLAUDE.md 3."""
    for src in ("greenhouse", "lever", "ashby"):
        assert not looks_like_staffing_firm(
            "Acme Corp", "Our client is a leading MNC.", src)
