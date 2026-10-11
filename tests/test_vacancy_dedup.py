"""One vacancy, one listing, one document (2026-10-11).

The fingerprint keeps "India" and "Hyderabad, Telangana" apart on purpose
(canon_place), so the same Infosys "Azure DevOps Engineer" sat in the queue
twice and each copy cost 2 tailor calls. listing_key ignores location; prepare
and the listings use it.

Also here: the IMAP timeout, because the run that hung for five days on
2026-10-06 is the reason anyone looked at this.
"""
import json

from jobpipe import db
from jobpipe.normalize import collapse_duplicates, fingerprint, listing_key


def _seed(rows):
    """rows: (company, title, location, status, score). Returns ids in order."""
    db.init()
    with db.connect() as c:
        c.execute("DELETE FROM jobs")
    ids = []
    for i, (co, title, loc, status, score) in enumerate(rows):
        db.upsert_job({
            "fingerprint": fingerprint(co, title, loc), "source": "adzuna",
            "source_id": str(i), "company": co, "company_canonical": co.lower(),
            "title": title, "location": loc, "remote": 0, "url": f"http://x/{i}",
            "apply_url": None, "description": "d", "salary_raw": None, "posted_at": None,
        })
        with db.connect() as c:
            jid = c.execute("SELECT max(id) FROM jobs").fetchone()[0]
            c.execute("UPDATE jobs SET status=?, score=? WHERE id=?", (status, score, jid))
        ids.append(jid)
    return ids


# --- the key ---------------------------------------------------------------
def test_location_and_noise_do_not_make_a_second_vacancy():
    assert listing_key("Infosys Ltd", "Azure DevOps Engineer") == \
        listing_key("Infosys", "Azure DevOps Engineer (Remote)")


def test_seniority_still_does():
    """Senior SRE and SRE at one employer are two jobs (is_near_duplicate)."""
    assert listing_key("Acme", "Senior SRE") != listing_key("Acme", "SRE")


# --- the listing -----------------------------------------------------------
def test_a_twin_is_folded_into_the_better_card_not_dropped_silently():
    jobs = [
        {"id": 1, "company": "Infosys", "title": "Azure DevOps Engineer",
         "location": "Hyderabad, Telangana", "score": 70, "url": "a", "source": "adzuna"},
        {"id": 2, "company": "Infosys", "title": "Azure DevOps Engineer",
         "location": "India", "score": 60, "url": "b", "source": "adzuna"},
        {"id": 3, "company": "Infosys", "title": "Cloud Engineer",
         "location": "Pune", "score": 50, "url": "c", "source": "adzuna"},
    ]
    out, hidden = collapse_duplicates(jobs)
    assert [j["id"] for j in out] == [1, 3], "a different role at the same company must stay"
    assert hidden == 1
    assert out[0]["location"] == "Hyderabad, Telangana (also: India)"
    assert out[0]["also"][0]["id"] == 2


def test_a_twin_of_an_applied_vacancy_is_not_listed():
    jobs = [{"id": 9, "company": "ProArch", "title": "Cloud Support Engineer",
             "location": "India", "score": 80, "url": "u", "source": "adzuna"}]
    out, hidden = collapse_duplicates(jobs, {listing_key("ProArch", "Cloud Support Engineer")})
    assert out == [] and hidden == 1


def test_review_queue_lists_each_vacancy_once():
    from jobpipe.review_api import queue
    ids = _seed([
        ("Infosys", "Azure DevOps Engineer", "Hyderabad, Telangana", "queued", 70),
        ("Infosys", "Azure DevOps Engineer", "India", "queued", 60),
        ("FCOOS", "Linux Cloud Engineer", "Bengaluru", "applied", 75),
        ("FCOOS", "Linux Cloud Engineer", "India", "queued", 65),
    ])
    rows = json.loads(bytes(queue().body).decode())
    assert [r["id"] for r in rows] == [ids[0]]
    assert "also: India" in rows[0]["location"]


# --- prepare ---------------------------------------------------------------
def test_prepare_skips_a_twin_of_a_queued_job_and_a_twin_in_its_own_batch():
    from jobpipe.tailor import candidates
    ids = _seed([
        ("Capco", "Azure DevOps Engineer", "Pune", "queued", 70),
        ("Capco", "Azure DevOps Engineer", "India", "shortlisted", 65),   # twin of queued
        ("PDI", "DevOps Engineer II", "Bengaluru", "shortlisted", 60),
        ("PDI", "DevOps Engineer II", "Hyderabad", "shortlisted", 55),     # twin in batch
        ("Siemens", "Senior DevOps Engineer", "Pune", "shortlisted", 50),
    ])
    assert [r["id"] for r in candidates(10)] == [ids[2], ids[4]]
    assert [r["id"] for r in candidates(1)] == [ids[2]]


# --- the hang --------------------------------------------------------------
def test_imap_connection_has_a_timeout(monkeypatch):
    from jobpipe import gmail
    seen = {}

    class Fake:
        def __init__(self, host, port, **kw):
            seen.update(kw)

        def login(self, *a):
            return "OK", []

    monkeypatch.setenv("GMAIL_ADDRESS", "a@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcdefghijklmnop")
    monkeypatch.setattr(gmail.imaplib, "IMAP4_SSL", Fake)
    gmail.connect()
    assert seen.get("timeout") and seen["timeout"] > 0, "imaplib waits forever without one"
