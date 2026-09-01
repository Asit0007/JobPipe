"""The alert channel's capacity gates (measured 2026-08-31).

Three separate gates were silently discarding work, and none of them logged
anything -- which is this channel's entire bug history (7.2, 7.25, 7.34, 7.38):
it produced less than it should and reported success.

  1. `search(QUERY, max_results=25)` was hardcoded while 62 alert emails were
     arriving in a 3-day window. `_search_imap` keeps `uids[-max_results:]`,
     the NEWEST 25, so 37 were dropped per run. Glassdoor sends over half the
     volume, so what got evicted was LinkedIn's and Naukri's mail -- the two
     boards carrying the Indian market.
  2. `DEFAULT_QUERY` searched four hardcoded sender domains, so an alert from
     any other portal was never even looked at.
  3. A posting whose link matched no `JOB_LINK_HINTS` entry was dropped into a
     single untyped counter, so "23 dropped" could not name the silent portal.
"""
import sqlite3

import pytest

from jobpipe.sources import gmail_alerts
from jobpipe.sources.gmail_alerts import DEFAULT_QUERY, _job_links, link_hints


# --- 1. the query reaches every portal, and fails open ----------------------

@pytest.mark.parametrize("domain", [
    "linkedin.com", "naukri.com", "indeed.com", "glassdoor.com",
    "glassdoor.co.in", "foundit.in", "instahyre.com", "cutshort.io",
    "hirist.tech", "shine.com", "timesjobs.com", "wellfound.com",
    "talent500.co",
])
def test_every_planned_portal_is_searched_by_domain(domain):
    assert f"from:{domain}" in DEFAULT_QUERY


def test_the_label_is_ORed_with_the_domains_never_replacing_them():
    """A bare `label:` query loses any alert that missed the label.

    Not hypothetical. Measured against the live mailbox 2026-08-31: the label
    held 58 messages, the domain list 64, and the 6 it missed were every
    LinkedIn alert in the window -- the user's filter does not route
    linkedin.com into the label. Dropping the domains would have zeroed the
    channel the plan calls the biggest unclaimed win.
    """
    assert 'label:"Job Notifications"' in DEFAULT_QUERY
    assert " OR " in DEFAULT_QUERY
    assert "from:linkedin.com" in DEFAULT_QUERY


# --- 2. the message cap is configurable and audible -------------------------

def _capture(monkeypatch, *, arriving, env_cap=None):
    """Run fetch() against a fake mailbox; return (cap_asked_for, log lines)."""
    seen = {}

    def fake_search(query, max_results):
        seen["cap"] = max_results
        return [f"msg{i}" for i in range(min(arriving, max_results))]

    monkeypatch.setattr("jobpipe.gmail.search", fake_search)
    monkeypatch.setattr("jobpipe.gmail.body_text", lambda m: "")
    monkeypatch.setattr("jobpipe.gmail.headers", lambda m: {"from": "x@indeed.com"})
    if env_cap is None:
        monkeypatch.delenv("GMAIL_ALERT_MAX", raising=False)
    else:
        monkeypatch.setenv("GMAIL_ALERT_MAX", str(env_cap))

    lines = []
    gmail_alerts.fetch(log=lines.append)
    return seen.get("cap"), lines


def test_the_cap_is_no_longer_hardcoded_at_25(monkeypatch):
    cap, _ = _capture(monkeypatch, arriving=10)
    assert cap != 25, "the 25-message cap is back; 62 emails arrive in 3 days"
    assert cap == 60


def test_the_cap_is_overridable_from_the_environment(monkeypatch):
    cap, _ = _capture(monkeypatch, arriving=10, env_cap=120)
    assert cap == 120


def test_hitting_the_cap_says_so(monkeypatch):
    """Truncation must never again be invisible.

    `make gmail-imap-check` reporting "25 message(s) matched" was the cap being
    hit, and it read like a measurement of the inbox.
    """
    _, lines = _capture(monkeypatch, arriving=500, env_cap=30)
    assert any("AT THE CAP" in ln for ln in lines), lines


def test_not_hitting_the_cap_stays_quiet(monkeypatch):
    _, lines = _capture(monkeypatch, arriving=5, env_cap=60)
    assert not any("AT THE CAP" in ln for ln in lines), lines
    assert any("of max 60" in ln for ln in lines), lines


# --- 3. link hints are extensible without a release -------------------------

FOUNDIT_EMAIL = (
    "Your job matches\n"
    "https://www.foundit.in/job-detail/devops-engineer-99 DevOps Engineer at Acme\n"
)


def test_an_unknown_portals_links_are_invisible_by_default(monkeypatch):
    monkeypatch.delenv("GMAIL_EXTRA_LINK_HINTS", raising=False)
    assert _job_links(FOUNDIT_EMAIL) == []


def test_a_measured_hint_from_the_environment_is_honoured(monkeypatch):
    monkeypatch.setenv("GMAIL_EXTRA_LINK_HINTS", "foundit.in/job-detail")
    assert [u for _, u in _job_links(FOUNDIT_EMAIL)] == [
        "https://www.foundit.in/job-detail/devops-engineer-99"
    ]


def test_hints_are_matched_case_insensitively(monkeypatch):
    """7.35: the real Glassdoor link was `.../partner/jobListing.htm`.

    The hint list held the lowercase `.com` spelling -- wrong case AND wrong
    TLD -- so every Glassdoor link was skipped. Whatever a user pastes into
    .env gets lowercased on both sides.
    """
    monkeypatch.setenv("GMAIL_EXTRA_LINK_HINTS", "  FoundIt.IN/Job-Detail  ")
    assert len(_job_links(FOUNDIT_EMAIL)) == 1


def test_an_empty_setting_adds_nothing(monkeypatch):
    monkeypatch.setenv("GMAIL_EXTRA_LINK_HINTS", "")
    monkeypatch.delenv("GMAIL_ALERT_MAX", raising=False)
    assert link_hints() == gmail_alerts.JOB_LINK_HINTS


def test_the_builtin_hints_are_never_lost(monkeypatch):
    monkeypatch.setenv("GMAIL_EXTRA_LINK_HINTS", "foundit.in/job-detail")
    for hint in gmail_alerts.JOB_LINK_HINTS:
        assert hint in link_hints()


# --- 4. a silent portal names itself ----------------------------------------

def _run_alert(monkeypatch, body, sender, titles, hints=None):
    if hints is None:
        monkeypatch.delenv("GMAIL_EXTRA_LINK_HINTS", raising=False)
    else:
        monkeypatch.setenv("GMAIL_EXTRA_LINK_HINTS", hints)
    monkeypatch.setattr("jobpipe.gmail.search", lambda q, max_results: ["m"])
    monkeypatch.setattr("jobpipe.gmail.body_text", lambda m: body)
    monkeypatch.setattr("jobpipe.gmail.headers", lambda m: {"from": sender})
    monkeypatch.setattr(gmail_alerts, "generate_json", lambda *a, **k: {
        "jobs": [{"title": t, "company": "Acme"} for t in titles]})
    lines = []
    jobs = gmail_alerts.fetch(log=lines.append)
    return jobs, lines


def test_an_unrecognised_host_is_reported_as_a_MISSING_HINT(monkeypatch):
    """No recognisable link in the email at all -- a hint really is missing."""
    jobs, lines = _run_alert(monkeypatch, FOUNDIT_EMAIL, "alerts@foundit.in",
                             ["DevOps Engineer"])
    assert jobs == [], "a posting with no confident link must be dropped"
    hint = [ln for ln in lines if "no recognisable" in ln]
    assert hint, lines
    assert "foundit" in hint[0], "the log must name the board"
    assert "GMAIL_EXTRA_LINK_HINTS" in hint[0], "and say how to fix it"


def test_a_known_host_that_cannot_pair_a_TITLE_is_not_blamed_on_hints(monkeypatch):
    """The 88-of-97 case, measured 2026-09-01.

    Glassdoor/Indeed/LinkedIn already had hints, yet the log told you to add
    one. The real fault is _match_link refusing to guess (7.3): the model
    paraphrased the title, or no unclaimed link sits within MAX_LINK_DISTANCE.
    A wrong diagnostic is worse than none -- 7.32.
    """
    body = ("Jobs for you\n"
            "https://linkedin.com/jobs/view/111 Senior DevOps Engineer at Acme\n")
    jobs, lines = _run_alert(monkeypatch, body, "jobalerts-noreply@linkedin.com",
                             ["Totally Paraphrased Title"])
    assert jobs == [], "a paraphrased title must not be paired to a link"
    blame = [ln for ln in lines if "GMAIL_EXTRA_LINK_HINTS" in ln]
    assert not blame, f"must NOT blame hints when links were present: {blame}"
    got = [ln for ln in lines if "no confident title match" in ln]
    assert got, lines
    assert "linkedin" in got[0]


def test_wellfound_is_a_named_board_not_anonymous_email(monkeypatch):
    """Unmapped senders land as `alert:email`, hiding their per-source yield."""
    body = ("New matches\n"
            "https://wellfound.com/jobs?job_listing_slug=123-devops-engineer"
            " DevOps Engineer at Acme\n")
    jobs, _ = _run_alert(monkeypatch, body, '"Wellfound" <team@hi.wellfound.com>',
                         ["DevOps Engineer"],
                         hints="wellfound.com/jobs?job_listing_slug")
    assert len(jobs) == 1, "the measured Wellfound link must be recognised"
    assert jobs[0]["source"] == "alert:wellfound", jobs[0]["source"]


# --- 5. the two config gaps the runbook exposed -----------------------------

def test_the_example_profile_accepts_the_target_cities():
    """Asserted against the TEMPLATE, never the user's private config.

    CI runs `make config`, which generates config from the *.example.yaml
    files, so a test that reads config/profile.yaml passes locally and fails
    in CI for reasons unrelated to the code. That trap has now caught
    test_facts_gate.py and test_every_skill_in_facts_yaml_lands_in_a_row.
    """
    import pathlib

    import yaml

    example = pathlib.Path(__file__).resolve().parents[1] / "config" / "profile.example.yaml"
    locations = yaml.safe_load(example.read_text())["identity"]["locations_ok"]
    lowered = {str(x).lower() for x in locations}
    for city in ("bengaluru", "bhubaneswar", "dehradun", "remote"):
        assert city in lowered, f"{city} missing -- the scorer is told nothing about it"


def test_a_remote_location_earns_the_bonus():
    from jobpipe.score import REMOTE_BONUS, apply_penalties

    job = {"title": "DevOps Engineer", "description": "", "company": "Acme",
           "source": "greenhouse", "location": "Remote - India"}
    score, applied = apply_penalties(70, job)
    assert score == 70 + REMOTE_BONUS
    assert any("remote" in a for a in applied)


def test_remote_in_the_DESCRIPTION_earns_nothing():
    """Bug 7.18's lesson, applied before it could happen again.

    "remote" is ordinary prose in a JD -- "remote hands", "supporting remote
    teams", "remote desktop" -- and appears in postings for fully onsite roles.
    Only the location field carries the signal.
    """
    from jobpipe.score import apply_penalties

    job = {"title": "DevOps Engineer", "company": "Acme", "source": "greenhouse",
           "location": "Bengaluru, Karnataka",
           "description": "Provide remote hands support to remote teams over "
                          "remote desktop from our Bengaluru office."}
    score, applied = apply_penalties(70, job)
    assert score == 70, f"description prose moved the score: {applied}"


def test_a_row_without_a_location_column_does_not_crash():
    """sqlite3.Row has no .get() and raises IndexError, not KeyError.

    The same shape that made claims.gate() crash on every prepare run while
    every test passed, because the tests all used dicts.
    """
    from jobpipe.score import apply_penalties

    assert apply_penalties(70, {"title": "DevOps Engineer", "description": "",
                                "company": "Acme", "source": "greenhouse"})[0] == 70


def test_a_real_sqlite3_row_gets_the_bonus():
    from jobpipe.score import REMOTE_BONUS, apply_penalties

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "select 'DevOps Engineer' as title, '' as description, 'Acme' as company,"
        " 'greenhouse' as source, 'Remote' as location"
    ).fetchone()
    assert apply_penalties(70, row)[0] == 70 + REMOTE_BONUS


# --- 6. remote reads the schema's flag, not a second derivation -------------

def _row(**cols):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    base = {"title": "DevOps Engineer", "description": "", "company": "Acme",
            "source": "greenhouse", "location": "Bengaluru, Karnataka"}
    base.update(cols)
    sel = ", ".join(f":{k} as {k}" for k in base)
    return conn.execute(f"select {sel}", base).fetchone()


def test_the_stored_remote_flag_wins_over_the_location_text():
    """The 29-row case: title says remote, location says India.

    "Senior AWS Cloud Engineer (Remote, Full-Time)" at location=India is
    exactly the India-located remote inventory this search wants, and a
    location-text match cannot see it.
    """
    from jobpipe.score import REMOTE_BONUS, apply_penalties

    job = _row(title="Senior AWS Cloud Engineer (Remote, Full-Time)",
               location="India", remote=1)
    score, applied = apply_penalties(70, job)
    assert score == 70 + REMOTE_BONUS, applied


def test_an_onsite_row_gets_nothing_even_if_its_text_says_remote():
    from jobpipe.score import apply_penalties

    job = _row(location="Bengaluru, Karnataka", remote=0,
               description="Provide remote hands support to remote teams.")
    assert apply_penalties(70, job)[0] == 70


def test_the_flag_is_not_re_derived_when_it_is_present():
    """remote=0 must beat a location that reads 'Remote'.

    Not a hypothetical preference -- it is what "one definition, shared"
    means. Two sources of truth that disagree is the bug being fixed.
    """
    from jobpipe.score import apply_penalties

    assert apply_penalties(70, _row(location="Remote", remote=0))[0] == 70
