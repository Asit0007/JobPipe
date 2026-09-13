"""Telegram silently dropped a job, and the run reported success.

Measured 2026-09-13: the scheduled run printed "queued 15 for review" while
only 14 rows got a notified_at. The 15th was
`MyCareernet - Linux Administration_94357`, and the underscore opens an italic
run that never closes in Telegram's legacy Markdown:

    400 Bad Request: can't parse entities: Can't find end of the entity
    starting at byte offset 223

Two independent faults: the message could not be sent, and the count reported
attempts rather than deliveries so nothing said so.
"""
import jobpipe.notify as notify


def test_an_underscore_in_a_title_is_escaped():
    """The exact string that failed."""
    assert notify._md("Linux Administration_94357") == r"Linux Administration\_94357"


def test_every_legacy_markdown_special_is_escaped():
    for ch in ("_", "*", "`", "["):
        assert notify._md(f"a{ch}b") == f"a\\{ch}b"


def test_a_backslash_is_escaped_first():
    r"""Escaping \ last would turn \_ into \\_ and re-break the parse."""
    assert notify._md("a\\_b") == r"a\\\_b"


def test_none_and_empty_do_not_crash():
    assert notify._md(None) == ""
    assert notify._md("") == ""


def test_the_run_counts_DELIVERIES_not_attempts(monkeypatch, capsys):
    """The reporting half. 15 attempted / 14 delivered printed "queued 15"."""
    rows = [{"id": i, "score": 70, "title": f"T{i}", "company": "C",
             "location": "Bangalore", "score_reason": "r",
             "missing_skills": "[]", "red_flags": "[]",
             "apply_url": "http://x", "url": "http://x"} for i in range(3)]
    monkeypatch.setattr(notify.db, "fetch", lambda **k: rows)
    monkeypatch.setattr(notify.db, "update", lambda *a, **k: None)
    monkeypatch.setattr(notify.db, "log_run", lambda *a, **k: None)
    monkeypatch.setattr(notify.cooldown, "applied_index", lambda: {})
    monkeypatch.setattr(notify.cooldown, "in_flight_index", lambda: {})
    monkeypatch.setattr(notify, "profile", lambda: {"thresholds": {"notify_daily_cap": 15}})

    calls = {"n": 0}

    def one_fails(text):
        calls["n"] += 1
        return calls["n"] != 2          # the second send is refused

    monkeypatch.setattr(notify, "_send", one_fails)
    notify.run()
    out = capsys.readouterr().out
    assert "queued 2 for review" in out, out
    assert "1 refused by telegram" in out, out


def test_a_fully_successful_run_says_nothing_about_failures(monkeypatch, capsys):
    rows = [{"id": 1, "score": 70, "title": "T", "company": "C",
             "location": "B", "score_reason": "r", "missing_skills": "[]",
             "red_flags": "[]", "apply_url": "http://x", "url": "http://x"}]
    monkeypatch.setattr(notify.db, "fetch", lambda **k: rows)
    monkeypatch.setattr(notify.db, "update", lambda *a, **k: None)
    monkeypatch.setattr(notify.db, "log_run", lambda *a, **k: None)
    monkeypatch.setattr(notify.cooldown, "applied_index", lambda: {})
    monkeypatch.setattr(notify.cooldown, "in_flight_index", lambda: {})
    monkeypatch.setattr(notify, "profile", lambda: {"thresholds": {"notify_daily_cap": 15}})
    monkeypatch.setattr(notify, "_send", lambda text: True)
    notify.run()
    out = capsys.readouterr().out
    assert "queued 1 for review" in out
    assert "refused" not in out
