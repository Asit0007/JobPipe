"""One application per company, then a wait. Asit's rule, 2026-09-13.

Motivating evidence: 35 of 372 shortlisted rows sat at 15 companies already
applied to, and ProArch had been applied to twice on one day.

The rule NEVER blocks -- these tests pin that too, because a filter that
silently removes a role is this project's oldest failure mode.
"""
import datetime as dt

import pytest

from jobpipe import cooldown

TODAY = dt.date(2026, 9, 13)


def idx(company, applied, role="DevOps Engineer", manual=False, job_id=1):
    """applied_index() shape: canonical company -> applications, newest first."""
    from jobpipe.normalize import canon_company
    return {canon_company(company): [{"id": None if manual else job_id,
                                      "company": company, "applied": applied,
                                      "role": role, "note": "", "manual": manual}]}


def test_a_recent_application_puts_the_company_in_cooldown():
    flag = cooldown.check("Cloudflare", applied=idx("Cloudflare", dt.date(2026, 9, 9)),
                          in_flight={}, today=TODAY)
    assert flag["kind"] == "cooldown"
    assert flag["until"] == "2026-12-08"          # 90 days on
    assert flag["days_left"] == 86


def test_the_company_is_free_once_the_window_passes():
    old = TODAY - dt.timedelta(days=91)
    assert cooldown.check("Cloudflare", applied=idx("Cloudflare", old),
                          in_flight={}, today=TODAY) is None


def test_the_boundary_day_itself_is_still_blocked():
    """Exactly 90 days is the last day of the wait, not the first free one."""
    exactly = TODAY - dt.timedelta(days=90)
    assert cooldown.check("X", applied=idx("X", exactly), in_flight={}, today=TODAY) is None
    day_before = TODAY - dt.timedelta(days=89)
    assert cooldown.check("X", applied=idx("X", day_before), in_flight={}, today=TODAY)


def test_company_names_match_canonically():
    """"Infosys Limited" and "Infosys" are one company -- measured, both appear."""
    applied = idx("Infosys", dt.date(2026, 9, 1))
    for variant in ("Infosys Limited", "INFOSYS", "Infosys Technologies Pvt Ltd"):
        assert cooldown.check(variant, applied=applied, in_flight={}, today=TODAY)


def test_a_document_does_not_cite_ITSELF_as_the_prior_application():
    """An applied row IS the most recent application to its own company.

    A "latest only" index made every such document announce that you had
    applied to this company, naming the very role the document was for. 48 of
    101 documents said it before this was fixed.
    """
    applied = idx("Cloudflare", dt.date(2026, 9, 9), job_id=7)
    assert cooldown.check("Cloudflare", job_id=7, applied=applied,
                          in_flight={}, today=TODAY) is None


def test_but_it_DOES_cite_an_earlier_application_to_the_same_company():
    """The genuinely useful case: this is the second role at that company."""
    from jobpipe.normalize import canon_company
    applied = {canon_company("Cloudflare"): [
        {"id": 7, "company": "Cloudflare", "applied": dt.date(2026, 9, 9),
         "role": "Role B", "note": "", "manual": False},
        {"id": 3, "company": "Cloudflare", "applied": dt.date(2026, 8, 20),
         "role": "Role A", "note": "", "manual": False},
    ]}
    flag = cooldown.check("Cloudflare", job_id=7, applied=applied,
                          in_flight={}, today=TODAY)
    assert flag["kind"] == "cooldown"
    assert flag["role"] == "Role A", "should fall through to the runner-up"


def test_a_manual_entry_still_applies_to_every_job():
    """Manual entries have no id, so they are never "the row in question"."""
    applied = idx("Acme", dt.date(2026, 9, 1), manual=True)
    assert cooldown.check("Acme", job_id=7, applied=applied,
                          in_flight={}, today=TODAY)
    assert cooldown.check("Acme", applied=applied, in_flight={}, today=TODAY)


def test_an_untouched_company_is_clear():
    assert cooldown.check("Nonesuch", applied=idx("Other", TODAY),
                          in_flight={}, today=TODAY) is None


def test_an_application_with_no_date_warns_forever_rather_than_going_quiet():
    """A missing date must not read as "no cooldown". Silence is the wrong failure."""
    flag = cooldown.check("Acme", applied=idx("Acme", None), in_flight={}, today=TODAY)
    assert flag["kind"] == "cooldown"
    assert flag["until"] is None
    assert "no date" in cooldown.line(flag).lower()


def test_an_unsent_document_is_a_DIFFERENT_signal_from_a_cooldown():
    flight = {"cloudflare": {"company": "Cloudflare", "role": "SRE",
                             "status": "queued", "id": 7}}
    flag = cooldown.check("Cloudflare", applied={}, in_flight=flight, today=TODAY)
    assert flag["kind"] == "in_queue"
    assert "not yet applied" in cooldown.line(flag)


def test_a_job_does_not_flag_ITSELF_as_being_in_the_queue():
    """Every queued row is in_flight, so without this every card warns on itself."""
    flight = {"cloudflare": {"company": "Cloudflare", "role": "SRE",
                             "status": "queued", "id": 7}}
    assert cooldown.check("Cloudflare", job_id=7, applied={},
                          in_flight=flight, today=TODAY) is None


def test_an_actual_application_outranks_an_unsent_document():
    """Having applied is the stronger fact; do not report the weaker one."""
    flight = {"cloudflare": {"company": "Cloudflare", "role": "SRE",
                             "status": "queued", "id": 7}}
    flag = cooldown.check("Cloudflare", applied=idx("Cloudflare", dt.date(2026, 9, 9)),
                          in_flight=flight, today=TODAY)
    assert flag["kind"] == "cooldown"


def test_a_manual_entry_is_labelled_as_such():
    flag = cooldown.check("Acme", applied=idx("Acme", dt.date(2026, 9, 1), manual=True),
                          in_flight={}, today=TODAY)
    assert flag["manual"] is True
    assert "by hand" in cooldown.line(flag)


@pytest.mark.parametrize("company", ["", None, "   "])
def test_a_blank_company_never_raises(company):
    assert cooldown.check(company, applied={}, in_flight={}, today=TODAY) is None


def test_line_renders_nothing_for_no_flag():
    assert cooldown.line(None) == ""


# --------------------------------------------------------------------------
# The rule NEVER removes a role. These are the tests that matter most: a
# filter that silently drops a job is indistinguishable from a pipeline that
# found nothing, and that is the failure this project has hit six times.
# --------------------------------------------------------------------------
def _seed_two_jobs():
    from jobpipe import db
    from jobpipe.normalize import fingerprint
    db.init()
    with db.connect() as c:
        c.execute("DELETE FROM jobs")
    for i, (co, status) in enumerate([("Cloudflare", "applied"),
                                      ("Cloudflare", "prepared")]):
        db.upsert_job({
            "fingerprint": fingerprint(co, f"Role {i}", "Bangalore"),
            "source": "test", "source_id": str(i), "company": co,
            "company_canonical": "cloudflare", "title": f"Role {i}",
            "location": "Bangalore", "remote": 0, "url": f"http://x/{i}",
            "apply_url": None, "description": "d", "salary_raw": None,
            "posted_at": None,
        })
    with db.connect() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM jobs ORDER BY id")]
        c.execute("UPDATE jobs SET status='applied', applied_at='2026-09-09' "
                  "WHERE id=?", (ids[0],))
        c.execute("UPDATE jobs SET status='prepared' WHERE id=?", (ids[1],))
    return ids


def test_a_role_at_a_cooldown_company_STILL_REACHES_THE_QUEUE():
    """Flag only. The second Cloudflare role must still be reviewable."""
    from jobpipe.review_api import queue
    ids = _seed_two_jobs()
    import json as _json
    rows = _json.loads(bytes(queue().body).decode())
    assert [r["id"] for r in rows] == [ids[1]], "the prepared role was filtered out"
    assert rows[0]["cooldown"]["kind"] == "cooldown"
    assert rows[0]["cooldown"]["until"] == "2026-12-08"


def test_the_cooldown_module_exposes_no_filter():
    """Structural guard: if someone adds a filter here, they must think about it.

    The decision was explicit -- report, never remove -- so a helper that
    returns a pruned list should not appear by accident.
    """
    banned = [n for n in dir(cooldown)
              if n.startswith(("filter", "exclude", "drop", "block"))]
    assert not banned, f"cooldown must not filter: {banned}"
