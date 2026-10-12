"""The five global feed adapters.

These are aggregators, not company boards: no slug, nothing in companies.yaml.
Fixtures below are trimmed copies of the real payloads captured 2026-08-28, so
each test pins the quirk that actually caught me rather than a generic shape.
No network -- these must not go flaky in CI.
"""
import pytest

from jobpipe.sources import arbeitnow, himalayas, jobicy, remoteok, remotive

REQUIRED = {"fingerprint", "source", "source_id", "company", "company_canonical",
            "title", "location", "remote", "url", "apply_url", "description",
            "salary_raw", "posted_at"}


def _patch(monkeypatch, module, payload):
    monkeypatch.setattr(module, "get_json", lambda *a, **k: payload)


def test_remoteok_skips_the_legal_object(monkeypatch):
    """The feed is a bare list whose FIRST element is metadata, not a job."""
    _patch(monkeypatch, remoteok, [
        {"last_updated": 1787887671, "legal": "API Terms of Service: link back"},
        {"id": 1137162, "position": "Principal Engineer", "company": "AIWI",
         "location": "", "url": "https://remoteok.com/x", "description": "Go and k8s",
         "salary_min": 0, "salary_max": 0, "date": "2026-08-27T09:00:02+00:00"},
    ])
    jobs = remoteok.fetch(log=lambda *_: None)
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Principal Engineer"
    assert set(jobs[0]) == REQUIRED
    assert jobs[0]["location"] == "Remote"      # blank location is not unknown


def test_remoteok_reads_position_not_title(monkeypatch):
    _patch(monkeypatch, remoteok, [
        {"title": "WRONG", "position": "SRE", "company": "Acme",
         "url": "u", "description": "d", "id": 1},
    ])
    assert remoteok.fetch(log=lambda *_: None)[0]["title"] == "SRE"


def test_arbeitnow_unescapes_before_stripping(monkeypatch):
    """Descriptions arrive HTML-escaped; strip_html alone removes nothing."""
    _patch(monkeypatch, arbeitnow, {"data": [{
        "slug": "x-1", "company_name": "Acme", "title": "DevOps Engineer",
        "description": "&lt;p&gt;We run &lt;strong&gt;Kubernetes&lt;/strong&gt; on AWS&lt;/p&gt;",
        "remote": True, "url": "https://arbeitnow.com/x", "location": "Berlin",
        "created_at": 1787907305}]})
    d = arbeitnow.fetch(log=lambda *_: None)[0]["description"]
    assert "Kubernetes" in d and "AWS" in d
    assert "&lt;" not in d and "<p>" not in d and "strong" not in d


def test_arbeitnow_marks_remote_in_the_location(monkeypatch):
    _patch(monkeypatch, arbeitnow, {"data": [{
        "slug": "x", "company_name": "Acme", "title": "SRE", "description": "d",
        "remote": True, "url": "u", "location": "Berlin", "created_at": 1787907305}]})
    j = arbeitnow.fetch(log=lambda *_: None)[0]
    assert "Remote" in j["location"] and j["remote"] == 1


def test_arbeitnow_survives_a_junk_timestamp(monkeypatch):
    _patch(monkeypatch, arbeitnow, {"data": [{
        "slug": "x", "company_name": "Acme", "title": "SRE", "description": "d",
        "remote": False, "url": "u", "location": "Berlin", "created_at": "not-a-number"}]})
    assert arbeitnow.fetch(log=lambda *_: None)[0]["posted_at"] is None


def test_jobicy_camelcase_fields(monkeypatch):
    """Not one of these field names is the obvious guess."""
    _patch(monkeypatch, jobicy, {"jobs": [{
        "id": 151872, "url": "https://jobicy.com/jobs/1", "jobTitle": "Staff DevOps Engineer",
        "companyName": "Hubstaff", "jobGeo": "LATAM, Canada, USA",
        "jobDescription": "<p>Terraform and Docker</p>", "jobExcerpt": "short",
        "pubDate": "2026-08-27T16:11:54+00:00"}]})
    j = jobicy.fetch(log=lambda *_: None)[0]
    assert j["title"] == "Staff DevOps Engineer" and j["company"] == "Hubstaff"
    assert j["location"] == "LATAM, Canada, USA"
    assert "Terraform" in j["description"] and "<p>" not in j["description"]


def test_jobicy_falls_back_to_the_excerpt(monkeypatch):
    _patch(monkeypatch, jobicy, {"jobs": [{
        "id": 1, "url": "u", "jobTitle": "SRE", "companyName": "Acme",
        "jobGeo": "", "jobDescription": "", "jobExcerpt": "Linux and AWS"}]})
    j = jobicy.fetch(log=lambda *_: None)[0]
    assert "Linux" in j["description"] and j["location"] == "Remote"


def test_himalayas_uses_guid_as_the_id(monkeypatch):
    """There is no id field."""
    _patch(monkeypatch, himalayas, {"jobs": [{
        "title": "Platform Engineer", "companyName": "micro1",
        "description": "<h3>k8s</h3>", "guid": "https://himalayas.app/jobs/1",
        "applicationLink": "https://himalayas.app/apply/1",
        "locationRestrictions": [], "pubDate": 1787905246,
        "minSalary": 100, "maxSalary": 150, "currency": "USD", "salaryPeriod": "hourly"}]})
    j = himalayas.fetch(log=lambda *_: None)[0]
    assert j["source_id"] == "https://himalayas.app/jobs/1"
    assert j["location"] == "Remote"       # empty restrictions means anywhere
    assert j["apply_url"] == "https://himalayas.app/apply/1"
    assert "100" in j["salary_raw"] and "USD" in j["salary_raw"]


def test_himalayas_joins_location_restrictions(monkeypatch):
    _patch(monkeypatch, himalayas, {"jobs": [{
        "title": "SRE", "companyName": "Acme", "description": "d",
        "guid": "g", "locationRestrictions": ["India", "Singapore"]}]})
    assert himalayas.fetch(log=lambda *_: None)[0]["location"] == "India, Singapore"


def test_remotive_keeps_eligibility_as_the_location(monkeypatch):
    _patch(monkeypatch, remotive, {"jobs": [{
        "id": 1, "title": "Senior DevOps Engineer", "company_name": "Lemon.io",
        "candidate_required_location": "LATAM, Europe, USA",
        "url": "https://remotive.com/x", "description": "<p>AWS</p>",
        "salary": "", "publication_date": "2026-08-25T13:57:53"}]})
    j = remotive.fetch(log=lambda *_: None)[0]
    assert j["location"] == "LATAM, Europe, USA"
    assert j["salary_raw"] is None          # empty string, not ""


@pytest.mark.parametrize("module,payload", [
    (remotive,  {"jobs": [{"id": 1, "title": "", "company_name": "Acme"}]}),
    (remoteok,  [{"id": 1, "position": "SRE", "company": ""}]),
    (arbeitnow, {"data": [{"slug": "s", "title": "SRE", "company_name": ""}]}),
    (jobicy,    {"jobs": [{"id": 1, "jobTitle": "", "companyName": "Acme"}]}),
    (himalayas, {"jobs": [{"title": "SRE", "companyName": ""}]}),
])
def test_rows_missing_a_title_or_company_are_dropped(monkeypatch, module, payload):
    _patch(monkeypatch, module, payload)
    assert module.fetch(log=lambda *_: None) == []


@pytest.mark.parametrize("module", [remotive, remoteok, arbeitnow, jobicy, himalayas])
def test_a_dead_feed_does_not_kill_ingest(monkeypatch, module):
    """CLAUDE.md section 10: errors in an adapter must not kill the run."""
    def boom(*a, **k):
        raise ConnectionError("feed is down")
    monkeypatch.setattr(module, "get_json", boom)
    assert module.fetch(log=lambda *_: None) == []


def test_all_five_are_registered_and_not_authoritative():
    from jobpipe.sources import ALL_SOURCES
    from jobpipe.normalize import AUTHORITATIVE_SOURCES
    for name in ("remotive", "remoteok", "arbeitnow", "jobicy", "himalayas"):
        assert name in ALL_SOURCES
        # Aggregators repost; the fuzzy dedup check must stay switched on here.
        assert name not in AUTHORITATIVE_SOURCES


# --- We Work Remotely (RSS, added 2026-10-11) ---------------------------------
from jobpipe.sources import weworkremotely  # noqa: E402

WWR_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>We Work Remotely</title>
<link>https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss</link>
<item>
  <title>BeyondTrust: Cloud Engineer</title>
  <region>Anywhere in the World</region>
  <country>United Kingdom</country>
  <description>&lt;p&gt;Terraform &amp;amp; AWS&lt;/p&gt;</description>
  <pubDate>Sat, 10 Oct 2026 07:30:48 +0000</pubDate>
  <guid>https://weworkremotely.com/remote-jobs/beyondtrust-cloud-engineer</guid>
  <link>https://weworkremotely.com/remote-jobs/beyondtrust-cloud-engineer</link>
</item>
<item>
  <title>A title with no company separator</title>
  <guid>https://weworkremotely.com/remote-jobs/x</guid>
</item>
</channel></rss>"""


def test_wwr_splits_company_from_title_and_uses_the_hiring_region():
    """<country> is the company's HQ, not where you may work -- region is."""
    jobs = weworkremotely.parse(WWR_FEED)
    assert len(jobs) == 1, "an item without 'Company: Title' must be skipped, not guessed"
    j = jobs[0]
    assert set(j) == REQUIRED
    assert (j["company"], j["title"]) == ("BeyondTrust", "Cloud Engineer")
    assert j["location"] == "Remote (Anywhere in the World)" and j["remote"] == 1
    assert "United Kingdom" not in j["location"]
    assert j["description"].startswith("Hiring region: Anywhere in the World")
    assert "Terraform & AWS" in j["description"]       # double-escaped HTML unwrapped
    assert j["source_id"] == "beyondtrust-cloud-engineer"


def test_wwr_drops_repeats_across_its_two_feeds_and_survives_a_dead_one(monkeypatch):
    calls = []

    def fake(url):
        calls.append(url)
        if len(calls) == 1:
            raise RuntimeError("feed down")
        return WWR_FEED

    monkeypatch.setattr(weworkremotely, "get_text", fake)
    assert len(weworkremotely.fetch(log=lambda *_: None)) == 1
    calls.clear()
    monkeypatch.setattr(weworkremotely, "get_text", lambda url: WWR_FEED)
    assert len(weworkremotely.fetch(log=lambda *_: None)) == 1, "same job in both feeds counted twice"


def test_wwr_apply_link_is_a_free_employer_search_not_the_paid_wwr_apply():
    """Applying through WWR is paid (2026-10-12); reading the posting is not."""
    j = weworkremotely.parse(WWR_FEED)[0]
    assert j["url"] == "https://weworkremotely.com/remote-jobs/beyondtrust-cloud-engineer"
    assert j["apply_url"].startswith(weworkremotely.EMPLOYER_SEARCH)
    assert "weworkremotely" not in j["apply_url"]
    from urllib.parse import unquote_plus
    q = unquote_plus(j["apply_url"].split("q=", 1)[1])
    assert '"BeyondTrust"' in q and '"Cloud Engineer"' in q
