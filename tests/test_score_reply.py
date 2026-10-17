"""A malformed scorer reply skips one job, never the whole score stage.

2026-10-16: the model answered one job with a JSON array; `result.get` raised
AttributeError outside the per-job try and 504 jobs went unscored that day.
"""
import pytest

from jobpipe import score


@pytest.mark.parametrize("reply, expected", [
    ({"score": 60, "reason": "fit"}, 60),
    ({"score": "55"}, 55),
    ([{"score": 70}], 70),          # one object wrapped in an array
    ({}, 0),                        # missing score keeps the old default
])
def test_usable_replies(reply, expected):
    assert score._reply(reply)["score"] == expected


@pytest.mark.parametrize("reply", [
    [], [{"score": 1}, {"score": 2}], ["60"], "60", None, {"score": "high"}, {"score": None},
])
def test_unusable_replies_are_refused(reply):
    assert score._reply(reply) is None


def test_run_skips_a_bad_reply_and_scores_the_rest(monkeypatch):
    jobs = [
        {"id": 1, "title": "Bad reply", "company": "A", "location": "Pune", "description": "x"},
        {"id": 2, "title": "Good reply", "company": "B", "location": "Pune", "description": "y"},
    ]
    replies = iter([[{"score": 1}, {"score": 2}], {"score": 80, "reason": "good"}])
    updates, lines = [], []
    monkeypatch.setattr(score, "profile", lambda: {
        "thresholds": {"shortlist_min_score": 45},
        "identity": {"years_experience": 4, "locations_ok": ["Pune"]},
        "targets": {"titles": ["DevOps Engineer"]},
    })
    monkeypatch.setattr(score, "facts", lambda: {"skills": {}})
    monkeypatch.setattr(score.jdfetch, "run", lambda log: None)
    monkeypatch.setattr(score.db, "fetch", lambda **k: jobs)
    monkeypatch.setattr(score.db, "update", lambda job_id, **k: updates.append((job_id, k)))
    monkeypatch.setattr(score.db, "log_run", lambda *a: None)
    monkeypatch.setattr(score, "budget_remaining", lambda model: 100)
    monkeypatch.setattr(score, "prefilter", lambda job: (True, 2, ""))
    monkeypatch.setattr(score, "apply_penalties", lambda s, job: (s, []))
    monkeypatch.setattr(score, "generate_json", lambda *a, **k: next(replies))

    score.run(log=lines.append)

    assert [(i, u["status"], u["score"]) for i, u in updates] == [(2, "shortlisted", 80)]
    assert any("score failed for Bad reply" in line for line in lines)
    assert lines[-1].startswith("done: 1 scored")
