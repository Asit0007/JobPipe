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
