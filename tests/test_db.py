"""Backfill (7.2) and portable ordering (7.7)."""
import pytest

from jobpipe import db
from jobpipe.sources.base import make_job


@pytest.fixture(autouse=True)
def fresh_db():
    db.init()
    with db.connect() as c:
        c.execute("DELETE FROM jobs")
    yield


def _job(**kw):
    base = dict(source="greenhouse", source_id="1", company="Acme Ltd",
                title="DevOps Engineer", location="Bangalore",
                url="https://example.com/1", description="")
    base.update(kw)
    return make_job(**base)


def test_later_sighting_backfills_an_empty_description():
    assert db.upsert_job(_job(source="alert:linkedin", description="")) == "inserted"
    assert db.upsert_job(_job(description="Kubernetes and Terraform", salary_raw="18 LPA")) == "enriched"
    row = db.fetch()[0]
    assert row["description"] == "Kubernetes and Terraform"
    assert row["salary_raw"] == "18 LPA"


def test_backfill_never_overwrites_an_existing_value():
    db.upsert_job(_job(description="the original full JD"))
    db.upsert_job(_job(description="a worse truncated snippet"))
    assert db.fetch()[0]["description"] == "the original full JD"


def test_nothing_to_fill_reports_seen():
    db.upsert_job(_job(description="full JD"))
    assert db.upsert_job(_job(description="full JD")) == "seen"


def test_unscored_jobs_sort_last_without_nulls_last():
    # `ORDER BY ... NULLS LAST` needs SQLite >= 3.30 and threw on older boxes.
    db.upsert_job(_job(title="Scored Role", url="https://example.com/a"))
    db.upsert_job(_job(title="Unscored Role", url="https://example.com/b"))
    rows = db.fetch()
    db.update([r for r in rows if r["title"] == "Scored Role"][0]["id"], score=70)
    assert [r["title"] for r in db.fetch()] == ["Scored Role", "Unscored Role"]


def test_archive_stale_leaves_human_owned_rows_alone():
    db.upsert_job(_job(title="Old Discovered", url="https://example.com/c"))
    db.upsert_job(_job(title="Old Applied", url="https://example.com/d"))
    with db.connect() as c:
        c.execute("UPDATE jobs SET last_seen = ?", (db.days_ago(60),))
        c.execute("UPDATE jobs SET status = 'applied' WHERE title = 'Old Applied'")
    assert db.archive_stale(21) == 1
    with db.connect() as c:
        got = dict(c.execute("SELECT title, status FROM jobs").fetchall())
    assert got == {"Old Discovered": "stale", "Old Applied": "applied"}


def test_ats_rows_are_never_dropped_as_reposts():
    # Measured against a live board, the fuzzy rule folded "Senior Channel
    # Partner Manager" into "Channel Partner Manager". ATS boards do not repost.
    from jobpipe.cli import _is_repost
    db.upsert_job(_job(title="Channel Partner Manager", url="https://example.com/x"))
    senior = _job(source="ashby", title="Senior Channel Partner Manager",
                  url="https://example.com/y")
    assert not _is_repost(senior)


def test_aggregator_reposts_are_still_dropped():
    from jobpipe.cli import _is_repost
    db.upsert_job(_job(title="DevOps Engineer - Cloud", url="https://example.com/x"))
    repost = _job(source="adzuna", title="Cloud DevOps Engineer", url="https://example.com/y")
    assert _is_repost(repost)
