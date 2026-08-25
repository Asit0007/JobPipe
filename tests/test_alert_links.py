"""Link/posting pairing in job-alert emails (CLAUDE.md 7.3).

The old code paired links[i] to jobs[i]. One interleaved promo link shifted
every subsequent posting onto someone else's URL.
"""
from jobpipe.sources.gmail_alerts import _job_links, _match_link

EMAIL = (
    "Jobs picked for you\n"
    "https://linkedin.com/jobs/view/111 DevOps Engineer at Acme\n"
    "https://linkedin.com/premium/upsell Try Premium free\n"
    "https://linkedin.com/jobs/view/222 Site Reliability Engineer at Globex\n"
)


def test_promotional_links_are_not_treated_as_postings():
    assert [u for _, u in _job_links(EMAIL)] == [
        "https://linkedin.com/jobs/view/111",
        "https://linkedin.com/jobs/view/222",
    ]


def test_interleaved_promo_does_not_shift_the_pairing():
    links, claimed = _job_links(EMAIL), set()
    got = {}
    for title in ("DevOps Engineer", "Site Reliability Engineer"):
        got[title] = _match_link(title, EMAIL, links, claimed)
        claimed.add(got[title])
    assert got["DevOps Engineer"].endswith("/111")
    assert got["Site Reliability Engineer"].endswith("/222")


def test_a_paraphrased_title_yields_no_link_rather_than_a_wrong_one():
    assert _match_link("DevOps Eng.", EMAIL, _job_links(EMAIL), set()) == ""


def test_a_url_is_never_handed_to_two_postings():
    links = _job_links(EMAIL)
    first = _match_link("DevOps Engineer", EMAIL, links, set())
    assert _match_link("DevOps Engineer", EMAIL, links, {first}) != first
