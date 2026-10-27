"""Replies reach Telegram, once each, and a check that could not run says so.

Measured 2026-10-27: track.py downloaded the newest 40 messages of a 7-day
query that matched 362, so it read about 18 hours of mail. Over 60 days that
left three employers' status updates unrecorded, and recruiters writing
without an application behind them were never looked for. None of it was ever
sent anywhere: the stage only wrote a status column.
"""
from contextlib import contextmanager
from email.message import EmailMessage

import pytest

from jobpipe import db, gmail, notify, track


@pytest.fixture(autouse=True)
def fresh_db():
    db.init()
    with db.connect() as c:
        for table in ("jobs", "replies", "runs"):
            c.execute(f"DELETE FROM {table}")
    yield


@pytest.fixture
def telegram(monkeypatch):
    """Every message that would have gone to Telegram."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat")
    sent = []
    monkeypatch.setattr(notify, "_send", lambda text: sent.append(text) or True)
    return sent


def _applied(company="Ramp", title="Site Reliability Engineer",
             apply_url="https://jobs.ashbyhq.com/ramp/123") -> int:
    with db.connect() as c:
        cur = c.execute(
            "INSERT INTO jobs (fingerprint, source, company, company_canonical, title, "
            "url, apply_url, first_seen, last_seen, status, applied_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,'applied',?)",
            (f"fp-{company}-{title}", "ashby", company, company.lower(), title,
             apply_url, apply_url, db.now(), db.now(), "2026-10-01T00:00:00+00:00"))
        return cur.lastrowid


def _head(i, sender, subject, date="Mon, 26 Oct 2026 07:55:00 +0000"):
    return {"id": str(i), "headers": {"from": sender, "subject": subject, "date": date,
                                      "message-id": f"<m{i}@example.invalid>"}}


def _mail(subject, sender, body):
    m = EmailMessage()
    m["Subject"], m["From"] = subject, sender
    m.set_content(body)
    return m


class FakeGmail:
    """gmail.session() over a fixed mailbox; counts logins."""

    def __init__(self, monkeypatch, heads, bulk=(), primary_recruiter=(), bodies=None):
        self.heads, self.fetched, self.logins = heads, [], 0
        self.bulk, self.recruiter = set(bulk), set(primary_recruiter)
        self.bodies = bodies or {}
        monkeypatch.setattr(gmail, "configured", lambda: True)
        monkeypatch.setattr(gmail, "session", self.session)

    @contextmanager
    def session(self):
        self.logins += 1
        yield self

    def search_headers(self, query, max_results=500):
        self.query = query
        return list(self.heads)

    def search_ids(self, query, max_results=500):
        return self.bulk if "category:promotions OR" in query else self.recruiter

    def fetch(self, ids):
        self.fetched += list(ids)
        return {i: self.bodies.get(i) or _mail("s", "f@example.invalid", "body") for i in ids}


@pytest.fixture
def model(monkeypatch):
    """The classifier's answers by subject; records every call."""
    answers, calls = {}, []

    def fake(prompt, **kw):
        calls.append(prompt)
        for subject, kind in answers.items():
            if subject in prompt:
                return {"kind": kind}
        return {"kind": "noise"}

    monkeypatch.setattr(track, "generate_json", fake)
    fake.answers, fake.calls = answers, calls
    return fake


# --------------------------------------------------------------------------
# which messages count
# --------------------------------------------------------------------------
@pytest.mark.parametrize("addr,subject,expected", [
    ("priya.k0dgvzlmnvbq==@naukri.com", "✉️ Job | DevOps Engineer in Bengaluru", "naukri"),
    ("info@naukri.com", "Get up to 4x more profile views", None),
    ("naukrialerts@naukri.com", "check out jobs applied by others", None),
    ("jane+6a00ff11@reply.cutshort.io", "RE: Platform Engineer at Acme", "cutshort"),
    ("voila@alerts.cutshort.io", "You have a questionnaire pending", None),
    ("founder@cutshort.io", "Welcome to Cutshort", None),
    ("messages-noreply@linkedin.com", "Jane Doe sent you a new message", "linkedin"),
    ("jobalerts-noreply@linkedin.com", "Site Reliability Engineer at Ramp", None),
])
def test_a_person_writing_through_a_job_board_is_told_apart_from_its_alerts(addr, subject, expected):
    assert track._portal_person(addr, subject) == expected


def _sorted(sender, subject, *, applied=True, bulk=False, primary_recruiter=False):
    by_token = track._index(db.fetch(status="applied")) if applied else {}
    h = _head(1, sender, subject)
    return track._sort(h, {"1"} if bulk else set(), {"1"} if primary_recruiter else set(),
                       by_token)


def test_a_job_alert_naming_a_company_you_applied_to_is_not_a_reply():
    """The first dry run's biggest mistake: "<title> at <company>" from a board
    is two signals, and alerts were most of the matches."""
    _applied()
    assert _sorted("jobalerts-noreply@linkedin.com", "Site Reliability Engineer at Ramp") is None
    assert _sorted("noreply@glassdoor.com", "Site Reliability Engineer role at Ramp: you would be a great fit!") is None


def test_an_ats_relay_naming_the_employer_in_its_local_part_matches():
    """`<employer>@myworkday.com` with a bare "Your Application" subject scored
    one signal and was never recorded."""
    job_id = _applied(company="Acme Exchange Group", title="Cloud Engineer",
                      apply_url="https://acme.wd3.myworkday.com/x")
    bucket, _, job, why = _sorted("acmeexchange@myworkday.com", "Your Application")
    assert bucket == "application" and job["id"] == job_id, why
    assert any(w.startswith("ats-local") for w in why)


def test_a_shared_ats_address_does_not_name_an_employer():
    n, why = track._signals({"company": "Reply", "title": "DevOps Engineer",
                             "apply_url": "", "url": ""},
                            "no-reply@myworkday.com", "Your Application")
    assert not any(w.startswith("ats-local") for w in why)


def test_an_application_update_from_an_employer_you_applied_to_elsewhere_counts():
    bucket, _, job, _ = _sorted("careers@acme-recruiting.example", "Your application to Acme",
                                applied=False)
    assert bucket == "application" and job is None


def test_a_bank_is_also_an_employer():
    """The first cut skipped any sender containing "bank", which would have
    hidden every reply from a bank you applied to."""
    assert _sorted("careers@bigbank.example", "Your application to BigBank",
                   applied=False)[0] == "application"
    assert _sorted("cards@bigbank.example", "Your application for a credit card",
                   applied=False) is None


@pytest.mark.parametrize("sender,subject", [
    ("cards_bank@bank.example", "Status of your Credit Card Application ID 5469"),
    ("alerts@somebank.example", "Email Verification for your Digital Savings A/c application"),
    ("noreply@github.com", "[GitHub] A third-party OAuth application has been added to your account"),
    ("courses@learnmailer.example", "Turn Your Expertise into AI Solutions | Applied Generative AI"),
    ("ACMEORC@workflow.mail.ap1.cloud.oracle.com", "New job opportunities at Acme"),
])
def test_mail_that_only_looks_like_an_application_is_skipped(sender, subject):
    assert _sorted(sender, subject, applied=False) is None


def test_a_recruiter_in_the_primary_tab_counts_without_a_model():
    assert _sorted("jane@agency.example", "Opportunity - SRE III | Bangalore",
                   applied=False, primary_recruiter=True)[0] == "recruiter"


def test_a_newsletter_from_an_employer_you_applied_to_is_not_a_reply():
    _applied()
    assert _sorted("news@ramp.com", "Ramp: Site Reliability Engineer stories",
                   bulk=True) is None


# --------------------------------------------------------------------------
# the whole run
# --------------------------------------------------------------------------
def _mailbox(monkeypatch):
    heads = [
        _head(1, "Ramp Careers <careers@ramp.com>",
              "Your application to Ramp - Site Reliability Engineer"),
        _head(2, "no-reply@us.greenhouse-mail.io", "Thank you for applying to Acme"),
        _head(3, "Jane Doe <jane@agency.example>", "Opportunity - SRE III_role | Bangalore"),
        _head(4, "priya.k0dgvz==@naukri.com", "✉️ Job | DevOps Engineer in Bengaluru"),
        _head(5, "ravi.x9q2==@naukri.com", "✉️ Job | Linux Administrator in Pune"),
        _head(6, "alerts@somebank.example", "Your savings account application"),
        _head(7, "jobalerts-noreply@linkedin.com", "Site Reliability Engineer at Ramp"),
    ]
    return FakeGmail(monkeypatch, heads, primary_recruiter={"3"})


def test_new_replies_reach_telegram_once(monkeypatch, telegram, model):
    job_id = _applied()
    model.answers["Your application to Ramp"] = "interview"
    box = _mailbox(monkeypatch)

    track.run(log=lambda *a: None)

    assert len(telegram) == 1
    text = telegram[0]
    assert "*Interview* - Ramp" in text
    assert "*Recruiter* - Jane Doe" in text and r"SRE III\_role" in text
    assert "*Naukri recruiter mails (2):*" in text and "DevOps Engineer in Bengaluru" in text
    assert "Thank you for applying" not in text      # an acknowledgement is noise
    assert "savings account" not in text
    row = db.fetch(status="responded")[0]
    assert row["id"] == job_id and row["response_kind"] == "interview"
    # Only application mail is read in full or shown to the model.
    assert sorted(box.fetched) == ["1", "2"] and len(model.calls) == 2
    # One login per run: four in a few seconds were refused on 2026-10-27.
    assert box.logins == 1

    track.run(log=lambda *a: None)
    assert len(telegram) == 1, "the same mail was reported twice"
    assert len(model.calls) == 2, "the same mail was classified twice"


def test_the_whole_window_is_read_not_the_newest_few(monkeypatch, telegram, model):
    box = FakeGmail(monkeypatch, [])
    monkeypatch.setenv("TRACK_LOOKBACK_DAYS", "14")
    track.run(log=lambda *a: None)
    assert "newer_than:14d" in box.query


def test_a_refused_send_is_kept_for_the_next_run(monkeypatch, model):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    _applied()
    model.answers["Your application to Ramp"] = "interview"
    _mailbox(monkeypatch)
    monkeypatch.setattr(notify, "_send", lambda text: False)
    with pytest.raises(RuntimeError, match="telegram refused"):
        track.run(log=lambda *a: None)
    assert len(db.unsent_replies()) == 4      # interview, recruiter, two Naukri

    sent = []
    monkeypatch.setattr(notify, "_send", lambda text: sent.append(text) or True)
    track.run(log=lambda *a: None)
    assert len(sent) == 1 and "Jane Doe" in sent[0]
    assert db.unsent_replies() == []


def test_a_gmail_failure_is_reported_not_mistaken_for_a_quiet_inbox(monkeypatch, telegram):
    monkeypatch.setattr(gmail, "configured", lambda: True)

    @contextmanager
    def broken():
        raise gmail.GmailAuthError("Gmail IMAP login failed")
        yield

    monkeypatch.setattr(gmail, "session", broken)
    with pytest.raises(RuntimeError, match="gmail unavailable"):
        track.run(log=lambda *a: None)
    assert len(telegram) == 1 and "could not read Gmail" in telegram[0]
    with db.connect() as c:
        assert c.execute("SELECT ok FROM runs WHERE stage='track'").fetchone()[0] == 0


def test_gmail_not_set_up_is_skipped_quietly(monkeypatch, telegram):
    monkeypatch.setattr(gmail, "configured", lambda: False)
    track.run(log=lambda *a: None)
    assert telegram == []


def test_a_dry_run_writes_sends_and_asks_nothing(monkeypatch, telegram, model):
    _applied()
    _mailbox(monkeypatch)
    lines = []
    track.run(log=lines.append, dry_run=True)
    assert telegram == [] and model.calls == []
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM replies").fetchone()[0] == 0
    assert any("naukri" in l for l in lines)


@pytest.mark.parametrize("answer", [["not", "a", "dict"], {"kind": "maybe"}, RuntimeError("503")])
def test_an_unusable_model_answer_still_reports_the_mail(monkeypatch, answer):
    def fake(prompt, **kw):
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(track, "generate_json", fake)
    assert track._classify("a@b.example", "Your application", _mail("s", "f", "b")) == "update"


# --------------------------------------------------------------------------
# the message itself
# --------------------------------------------------------------------------
def _row(i, kind="recruiter", subject="Opportunity", sender="Jane <jane@agency.example>"):
    return {"message_id": f"m{i}", "kind": kind, "subject": subject, "sender": sender,
            "received_at": "2026-10-26T07:55:00+00:00", "job_id": None,
            "job_company": None, "job_title": None}


def test_a_long_alert_is_split_under_telegrams_limit():
    rows = [_row(i, subject="x" * 150) for i in range(60)]
    texts = track.messages(rows)
    assert len(texts) > 1
    assert all(len(t) <= track.TELEGRAM_LIMIT for t in texts)
    assert sum(t.count("*Recruiter*") for t in texts) == 60


def test_folded_headers_become_one_line():
    h = track._unfold(_head(1, "Some Company\r\n <jane@agency.example>", "Opportunity -\r\n  SRE"))
    assert h["headers"]["from"] == "Some Company <jane@agency.example>"
    assert h["headers"]["subject"] == "Opportunity - SRE"


def test_the_most_important_replies_come_first():
    text = track.messages([_row(1, "rejection"), _row(2, "offer"), _row(3, "interview")])[0]
    assert text.index("*Offer*") < text.index("*Interview*") < text.index("*Rejection*")


# --------------------------------------------------------------------------
# reading headers over IMAP
# --------------------------------------------------------------------------
class FakeIMAP:
    def __init__(self, uids):
        self.uids, self.fetches = uids, []

    def select(self, *a, **kw):
        return "OK", [b""]

    def uid(self, command, *args):
        if command == "SEARCH":
            return "OK", [b" ".join(self.uids)]
        self.fetches.append(args[0])
        out = []
        for u in reversed(args[0].split(b",")):         # any order, as a server may
            out.append((b"1 (UID " + u + b" BODY[HEADER.FIELDS (FROM)] {20}",
                        b"Subject: s" + u + b"\r\nMessage-ID: <" + u + b">\r\n\r\n"))
            out.append(b")")
        return "OK", out

    def close(self):
        pass

    def logout(self):
        pass


def test_headers_are_read_in_batches_and_returned_newest_first(monkeypatch):
    uids = [str(n).encode() for n in range(1, 451)]
    fake = FakeIMAP(uids)
    monkeypatch.setattr(gmail, "_app_password", lambda: ("a@gmail.com", "pw"))
    monkeypatch.setattr(gmail, "connect", lambda: fake)
    with gmail.session() as g:
        rows = g.search_headers("newer_than:14d", max_results=400)
    assert len(fake.fetches) == 2                      # 400 asked, 200 per FETCH
    assert [r["id"] for r in rows[:2]] == ["450", "449"]
    assert rows[-1]["id"] == "51"
    assert rows[0]["headers"]["message-id"] == "<450>"
