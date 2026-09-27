"""The model-usage log behind the dashboard's "AI models used, last 48 h" panel."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from jobpipe import site, usage

NOW = datetime(2026, 9, 27, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def logs(tmp_path, monkeypatch):
    jp = tmp_path / "jp.jsonl"
    cp_dir = tmp_path / "ContentPipe"
    (cp_dir / ".runs").mkdir(parents=True)
    monkeypatch.setenv("JOBPIPE_USAGE_LOG", str(jp))
    monkeypatch.setenv("CONTENTPIPE_DIR", str(cp_dir))
    return jp, cp_dir / ".runs" / "model-usage.jsonl"


def line(t, **kw):
    return json.dumps({"t": t.strftime("%Y-%m-%dT%H:%M:%SZ"), **kw}) + "\n"


def test_record_appends_one_line_per_call(logs):
    jp, _ = logs
    usage.record("groq", "qwen/qwen3.8-27b", "ok", ms=210)
    usage.record("gemini", "gemini-flash-latest", "overloaded", ms=900, detail="HTTP  503\n")
    rows = [json.loads(l) for l in jp.read_text().splitlines()]
    assert [r["outcome"] for r in rows] == ["ok", "overloaded"]
    assert rows[0]["app"] == "jobpipe" and rows[0]["ms"] == 210
    assert rows[1]["detail"] == "HTTP 503", "whitespace collapsed"


def test_both_apps_are_aggregated_per_model_inside_the_window_only(logs):
    jp, cp = logs
    jp.write_text(
        line(NOW - timedelta(hours=2), app="jobpipe", provider="gemini", model="gemini-flash-latest", outcome="ok")
        + line(NOW - timedelta(hours=1), app="jobpipe", provider="gemini", model="gemini-flash-latest", outcome="overloaded", detail="HTTP 503")
        + line(NOW - timedelta(hours=60), app="jobpipe", provider="gemini", model="too-old", outcome="ok")
        + "{torn line\n")
    cp.write_text(
        line(NOW - timedelta(hours=3), app="contentpipe", provider="hf", model="linoyts/wan2-2-i2v-rCM", outcome="ok", task="Video burn")
        + line(NOW - timedelta(hours=30), app="contentpipe", provider="groq", model="qwen/qwen3.8-27b", outcome="ok",
               count=7, source="reconstructed"))
    out = usage.aggregate(48, now=NOW)
    by = {(m["app"], m["model"]): m for m in out["models"]}
    assert ("jobpipe", "too-old") not in by
    flash = by[("jobpipe", "gemini-flash-latest")]
    assert (flash["calls"], flash["ok"], flash["failed"]) == (2, 1, 1)
    assert flash["last_problem"] == "overloaded: HTTP 503"
    assert flash["last_used"] == "2026-09-27T14:00:00Z"
    qwen = by[("contentpipe", "qwen/qwen3.8-27b")]
    assert qwen["calls"] == 7 and qwen["reconstructed"] == 7
    assert by[("contentpipe", "linoyts/wan2-2-i2v-rCM")]["tasks"] == ["Video burn"]


def test_missing_logs_are_an_empty_panel_not_an_error(logs):
    assert usage.aggregate(48, now=NOW)["models"] == []


def test_a_log_write_failure_never_breaks_a_model_call(logs, monkeypatch):
    monkeypatch.setenv("JOBPIPE_USAGE_LOG", "/dev/null/cannot/exist.jsonl")
    usage.record("gemini", "x", "ok")          # must not raise


def test_the_api_route_returns_the_aggregate(logs):
    from jobpipe import review_api

    usage.record("groq", "qwen/qwen3.8-27b", "ok")
    body = json.loads(review_api.models(hours=48).body)
    assert [m["model"] for m in body["models"]] == ["qwen/qwen3.8-27b"]


# --- the panel is local only ------------------------------------------------------

def test_the_public_export_carries_no_models_panel():
    html = site._rewrite((site.TEMPLATE_DIR / "dashboard.html").read_text(), "x")
    assert "/api/models" not in html and "models-body" not in html and "local-only" not in html


def test_the_local_dashboard_still_has_it():
    html = (site.TEMPLATE_DIR / "dashboard.html").read_text()
    assert "/api/models" in html and "AI models used" in html


def test_an_unpaired_marker_fails_the_export_loudly():
    with pytest.raises(RuntimeError, match="without"):
        site._strip_local_only("<!-- local-only:start --> never closed")
    with pytest.raises(RuntimeError, match="without"):
        site._strip_local_only("stray // local-only:end")


def test_a_rotated_log_is_still_read_and_our_own_log_rotates(logs):
    jp, cp = logs
    cp.with_name(cp.name + ".1").write_text(
        line(NOW - timedelta(hours=5), app="contentpipe", provider="groq", model="old-half", outcome="ok"))
    jp.write_bytes(b"x" * usage.MAX_BYTES)
    usage.record("groq", "fresh", "ok")
    assert jp.with_name(jp.name + ".1").stat().st_size == usage.MAX_BYTES
    assert len(jp.read_text().splitlines()) == 1
    models = {m["model"] for m in usage.aggregate(48, now=NOW)["models"]}
    assert "old-half" in models
