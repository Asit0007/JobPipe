"""Reply matching needs two independent signals (CLAUDE.md 7.5).

Short company names -- ramp, linear, vanta -- used to match any mail that
happened to contain the word.
"""
from jobpipe.track import MIN_SIGNALS, _signals


class Row(dict):
    def __getitem__(self, k):
        return dict.get(self, k)


JOB = Row(company="Ramp", title="Site Reliability Engineer",
          apply_url="https://jobs.ashbyhq.com/ramp/123", url="")


def matches(sender, subject):
    n, _ = _signals(JOB, sender, subject)
    return n >= MIN_SIGNALS


def test_reply_from_the_employers_own_domain_matches():
    assert matches("careers@ramp.com", "Your application to Ramp - Site Reliability Engineer")


def test_reply_relayed_by_an_ats_matches():
    assert matches("no-reply@us.greenhouse-mail.io",
                   "Ramp: update on your Site Reliability Engineer application")


def test_unrelated_mail_containing_the_company_name_does_not_match():
    assert not matches("newsletter@techcrunch.com", "The on-ramp to better infrastructure")
    assert not matches("deals@shopping.com", "Ramp up your savings this weekend")


def test_a_product_newsletter_from_a_similarly_named_company_does_not_match():
    assert not matches("hello@linear.app", "Linear changelog for March")


def test_ats_sender_alone_is_never_enough():
    n, why = _signals(JOB, "no-reply@greenhouse.io", "An update on your application")
    assert why == ["ats-sender"] and n < MIN_SIGNALS
