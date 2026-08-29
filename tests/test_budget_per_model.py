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
    # B carries a real measured cap of 20 (7.33), which would otherwise swamp
    # the small numbers these tests use. Override it the documented way, which
    # exercises the GEMINI_RPD_BUDGET_<model> path at the same time.
    monkeypatch.setenv(f"GEMINI_RPD_BUDGET_{B}", "3")
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


def test_the_cap_is_per_model_not_just_the_counter(tmp_path, monkeypatch):
    """7.33. 7.26 split the COUNTER per model but left the CAP global, so
    `cli status` reported 481 calls left on a model Google had already cut off
    at 20. A counter that promises headroom the API has refused is worse than
    no counter, because it is trusted.
    """
    monkeypatch.setattr(llm, "STATE_FILE", tmp_path / "budget.json")
    monkeypatch.setenv("GEMINI_RPM", "10000")
    monkeypatch.setenv("GEMINI_RPD_BUDGET", "500")

    # gemini-flash-latest resolves to gemini-3.7-flash, measured at 20/day.
    assert llm.budget_remaining(B) == 20, "must not inherit the 500 default"
    assert llm.budget_remaining(A) == 500, "flash-lite keeps the large default"

    for _ in range(20):
        llm._reserve_slot(B)
    with pytest.raises(llm.QuotaExhausted):
        llm._reserve_slot(B)

    # The status line must now say 0 for B, not 480.
    assert llm.budget_by_model()[B] == 0
    assert llm.budget_remaining(A) == 500      # A is untouched


def test_an_explicit_per_model_override_beats_the_measured_default(tmp_path, monkeypatch):
    """If Google raises the quota, .env must be able to say so without a code
    change -- and without editing DEFAULT_MODEL_CAPS."""
    monkeypatch.setattr(llm, "STATE_FILE", tmp_path / "budget.json")
    monkeypatch.setenv("GEMINI_RPM", "10000")
    monkeypatch.setenv("GEMINI_RPD_BUDGET", "500")
    monkeypatch.setenv(f"GEMINI_RPD_BUDGET_{B}", "50")
    assert llm.budget_remaining(B) == 50


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

    env = {**os.environ, "GEMINI_RPM": "100000", "GEMINI_RPD_BUDGET": "100000",
           f"GEMINI_RPD_BUDGET_{B}": "100000"}
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


# --- 7.28: the counter must roll on Google's day, not the machine's ----------

@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="POSIX only")
def test_quota_day_ignores_the_machine_timezone():
    """The discriminating test, and it has to be built carefully.

    Comparing _quota_day() against the Pacific date proves nothing for the
    hours when the local date happens to agree -- IST and Pacific share a date
    from 12:30 to 23:59 IST, so a naive date.today() passes such a test for
    half the day. Instead, evaluate it under two zones whose local dates can
    NEVER agree: UTC+14 and UTC-12 are 26 hours apart. A timezone-independent
    answer is equal under both; date.today() cannot be.
    """
    import os
    import time
    from datetime import datetime
    from zoneinfo import ZoneInfo

    original = os.environ.get("TZ")
    try:
        seen = []
        for tz in ("Etc/GMT-14", "Etc/GMT+12"):     # UTC+14, UTC-12
            os.environ["TZ"] = tz
            time.tzset()
            seen.append((llm._quota_day(), datetime.now().date().isoformat()))

        (quota_a, local_a), (quota_b, local_b) = seen
        assert local_a != local_b, "the two zones must straddle a date boundary"
        assert quota_a == quota_b, "quota day must not follow the machine clock"
        assert quota_a == datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()


def test_quota_day_falls_back_without_a_tz_database(budget, monkeypatch):
    """python:*-slim ships no /usr/share/zoneinfo. Degrade, do not crash."""
    import builtins
    real_import = builtins.__import__

    def no_zoneinfo(name, *a, **k):
        if name == "zoneinfo":
            raise ModuleNotFoundError("no tzdata")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_zoneinfo)
    day = llm._quota_day()
    monkeypatch.undo()

    from datetime import datetime, timedelta, timezone
    # PST, never PDT: rolling late under-allows; rolling early spends into a 429.
    assert day == datetime.now(timezone(timedelta(hours=-8))).date().isoformat()


def test_the_counter_resets_when_the_pacific_day_turns(budget, monkeypatch):
    monkeypatch.setattr(llm, "_quota_day", lambda: "2026-08-28")
    llm._reserve_slot(A)
    llm._reserve_slot(A)
    assert llm.budget_remaining(A) == 1

    monkeypatch.setattr(llm, "_quota_day", lambda: "2026-08-29")
    assert llm.budget_remaining(A) == 3      # fresh quota day
    llm._reserve_slot(A)
    assert llm.budget_remaining(A) == 2


def test_state_written_under_one_quota_day_is_ignored_by_the_next(budget, monkeypatch):
    monkeypatch.setattr(llm, "_quota_day", lambda: "2026-08-28")
    llm._reserve_slot(A)
    monkeypatch.setattr(llm, "_quota_day", lambda: "2026-08-29")
    llm._reserve_slot(A)
    state = json.loads(budget.read_text())
    assert state["date"] == "2026-08-29"
    assert state["models"][A]["count"] == 1
