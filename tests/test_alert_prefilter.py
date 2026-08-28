"""The carve-out for job-alert rows that arrived without a description.

Bug 7.2 said alert rows die in the prefilter counting zero keywords. jdfetch
fixed that for boards it can reach; it explicitly cannot reach LinkedIn,
Indeed or Glassdoor (login walls, and LinkedIn's robots.txt disallows
/jobs/view/ outright). Those rows stay description-less forever, so the
keyword gate has to stand aside for them or the whole alert channel is dead.

Measured on the 4,654-row corpus with descriptions stripped, 2026-08-28:
  - "DevOps Engineer"    -> 0 must-have keyword hits
  - "AWS Cloud Engineer" -> 1 hit
  both below keyword_prefilter_min=2, both silently killed.

The alternative -- gating on a title match against targets.titles -- was
measured and rejected: it kept 3 to 5 of the 17 rows that scored >= 45.
"""
from jobpipe.score import prefilter


def _job(**kw):
    row = {"title": "", "description": "", "company": "Acme",
           "location": "Bangalore", "source": "greenhouse"}
    row.update(kw)
    return row


def test_alert_row_without_jd_skips_the_keyword_gate():
    for title in ("DevOps Engineer", "Site Reliability Engineer",
                  "Cloud Operations Engineer", "Release Engineer"):
        ok, _, why = prefilter(_job(title=title, source="alert:linkedin"))
        assert ok, f"{title!r} was killed: {why}"


def test_the_same_row_from_an_ats_board_is_still_killed():
    """The carve-out is for alert rows only. An ATS row with no description is
    a broken ingest, not a channel to rescue."""
    ok, hits, why = prefilter(_job(title="DevOps Engineer", source="greenhouse"))
    assert not ok and hits == 0
    assert "must-have keyword" in why


def test_alert_row_WITH_a_description_faces_the_normal_gate():
    """Once jdfetch has filled the row in, it is an ordinary posting."""
    ok, _, _ = prefilter(_job(
        title="DevOps Engineer", source="alert:naukri",
        description="Wordpress and Photoshop work for a design studio."))
    assert not ok, "a filled-in alert row must not keep the carve-out"

    ok, hits, _ = prefilter(_job(
        title="DevOps Engineer", source="alert:naukri",
        description="You will run Kubernetes on AWS with Terraform."))
    assert ok and hits >= 2


def test_carve_out_does_not_bypass_the_safety_filters():
    """title_reject and hard_reject are the filters that actually matter here."""
    ok, _, why = prefilter(_job(title="Account Executive", source="alert:linkedin"))
    assert not ok and "title reject" in why

    ok, _, why = prefilter(_job(
        title="DevOps Engineer", source="alert:indeed",
        description="Requires 15+ years of experience."))
    assert not ok and "hard reject" in why


def test_whitespace_only_description_counts_as_missing():
    ok, _, _ = prefilter(_job(title="DevOps Engineer", source="alert:indeed",
                              description="   \n  "))
    assert ok
