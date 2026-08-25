"""The hard-reject proximity rule (CLAUDE.md 7.9).

A plain substring test on "10+ years" also killed postings whose JD merely
mentioned the team's combined experience. Those were good roles, discarded
silently, which is the expensive kind of wrong.
"""
from jobpipe.score import _hard_reject_hit


def test_real_experience_requirement_is_rejected():
    assert _hard_reject_hit("10+ years", "looking for 10+ years of experience in linux")
    assert _hard_reject_hit("10+ years", "minimum 10+ years exp. required")


def test_team_experience_is_not_a_requirement():
    assert not _hard_reject_hit(
        "10+ years", "our team has 10+ years of combined experience building infra")
    assert not _hard_reject_hit(
        "12+ years", "a company with a 12+ years track record of experience")


def test_years_without_experience_context_is_not_a_requirement():
    assert not _hard_reject_hit("10+ years", "the platform has served customers for 10+ years")


def test_non_years_terms_stay_plain_substring():
    assert _hard_reject_hit("voice process", "this is a voice process role")
    assert _hard_reject_hit("l1 support", "l1 support desk")
    assert not _hard_reject_hit("voice process", "backend engineer, no phone work")


class FakeJob(dict):
    def __getitem__(self, k):
        return dict.get(self, k)


def _job(title, description=""):
    return FakeJob(title=title, description=description)


# A GTM job description names your whole toolchain because it SELLS it. On a
# real 4,654-posting ingest these were 44% of everything reaching the LLM.
SELLS_YOUR_STACK = "Sell our Kubernetes, Terraform, AWS and Docker platform to enterprises."


def test_gtm_titles_are_rejected_however_technical_the_jd_sounds():
    from jobpipe.score import prefilter
    for title in ("Enterprise Account Executive", "Solutions Engineer, Upmarket",
                  "Senior Product Manager, Cloud Networking", "Technical Recruiter"):
        ok, _, why = prefilter(_job(title, SELLS_YOUR_STACK))
        assert not ok and why.startswith("title reject"), title


def test_real_infrastructure_titles_still_pass():
    from jobpipe.score import prefilter
    jd = "Own our Kubernetes clusters and Terraform modules. On-call rotation."
    for title in ("Site Reliability Engineer", "DevOps Engineer",
                  "Platform Engineer", "Senior Infrastructure Engineer"):
        ok, _, why = prefilter(_job(title, jd))
        assert ok, f"{title} was rejected: {why}"


def test_title_reject_beats_the_keyword_count():
    # The point of checking the title first: it is decisive and free.
    from jobpipe.score import prefilter
    ok, hits, _ = prefilter(_job("Account Executive", SELLS_YOUR_STACK))
    assert not ok and hits == 0
