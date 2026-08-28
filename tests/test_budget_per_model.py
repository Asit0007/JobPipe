"""The daily budget counter, split per model (CLAUDE.md 7.26).

Google's free-tier quotas are per project PER MODEL -- the 429 names them
`GenerateRequestsPerDayPerProjectPerModel-FreeTier`, measured at 500/day on
2026-08-28. One shared counter meant a long `score` run on flash-lite locked
out `prepare`, whose model had spent nothing.

The concurrency case matters more than it looks. Per CLAUDE.md 7.6 the original
flock'd counter looked correct and still lost 1,902 of 2,000 increments,
because a buffered write flushes at close() -- after the unlock. That had no
regression test until this file.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jobpipe import llm

A, B = "gemini-flash-lite-latest", "gemini-flash-latest"


@pytest.fixture
def budget(tmp_path, monkeypatch):
    """A private counter file. Never touch the real data/gemini_budget.json."""
    f = tmp_path / "budget.json"
    monkeypatch.setattr(llm, "STATE_FILE", f)
    monkeypatch.setenv("GEMINI_RPM", "10000")
    monkeypatch.setenv("GEMINI_RPD_BUDGET", "3")
    return f


def test_models_do_not_share_a_daily_budget(budget):
    for _ in range(3):
        llm._reserve_slot(A)
    with pytest.raises(llm.QuotaExhausted) as e:
        llm._reserve_slot(A)
    assert A in str(e.value)

    # B is untouched. This is the whole point: a spent `score` must not block
    # `prepare`, whose model has its own 500.
    llm._reserve_slot(B)
    assert llm.budget_remaining(A) == 0
    assert llm.budget_remaining(B) == 2


def test_budget_by_model_reports_each(budget):
    llm._reserve_slot(A)
    llm._reserve_slot(A)
    llm._reserve_slot(B)
    assert llm.budget_by_model() == {A: 1, B: 2}


def test_bare_budget_remaining_reports_the_tightest(budget):
    assert llm.budget_remaining() == 3          # nothing used yet
    llm._reserve_slot(A)
    llm._reserve_slot(A)
    llm._reserve_slot(B)
    # Tightest, not a total: "how many more am I sure of".
    assert llm.budget_remaining() == 1


def test_rpm_window_is_per_model_too(budget, monkeypatch):
    monkeypatch.setenv("GEMINI_RPM", "1")
    monkeypatch.setenv("GEMINI_RPD_BUDGET", "10")
    llm._reserve_slot(A)
    llm._reserve_slot(B)      # must not block behind A's minute window
    state = json.loads(budget.read_text())
    assert len(state["models"][A]["stamps"]) == 1
    assert len(state["models"][B]["stamps"]) == 1


def test_a_pre_split_state_file_is_discarded_not_guessed_at(budget):
    """Legacy {"count","stamps"} cannot say which model spent them. Attributing
    them to a model would lock out one whose quota is untouched."""
    from datetime import date
    budget.write_text(json.dumps(
        {"date": date.today().isoformat(), "count": 999, "stamps": []}))
    assert llm.budget_remaining(A) == 3
    llm._reserve_slot(A)
    assert llm.budget_remaining(A) == 2


def test_a_corrupt_state_file_does_not_crash(budget):
    budget.write_text("{not json")
    llm._reserve_slot(A)
    assert llm.budget_remaining(A) == 2


# --- the 7.6 case: concurrent processes must not lose increments -------------

CHILD = """
import sys
from pathlib import Path
sys.path.insert(0, {src!r})
from jobpipe import llm
llm.STATE_FILE = Path(sys.argv[1])
model = sys.argv[2]
for _ in range(int(sys.argv[3])):
    llm._reserve_slot(model)
"""


def test_concurrent_processes_lose_no_increments(tmp_path):
    """6 processes, 2 models, 60 increments each. Exact counts or bust.

    Catches both halves: a lost increment (the buffered-write bug) and an
    increment landing in the wrong model's bucket.
    """
    src = str(Path(__file__).resolve().parents[1] / "src")
    f = tmp_path / "budget.json"
    child = tmp_path / "child.py"
    child.write_text(CHILD.format(src=src))

    env = {**os.environ, "GEMINI_RPM": "100000", "GEMINI_RPD_BUDGET": "100000"}
    n_procs, per_proc = 6, 60
    procs = [subprocess.Popen([sys.executable, str(child), str(f),
                               A if i % 2 == 0 else B, str(per_proc)], env=env)
             for i in range(n_procs)]
    for p in procs:
        assert p.wait(timeout=120) == 0

    state = json.loads(f.read_text())
    expected = (n_procs // 2) * per_proc          # 3 processes per model
    assert state["models"][A]["count"] == expected
    assert state["models"][B]["count"] == expected
