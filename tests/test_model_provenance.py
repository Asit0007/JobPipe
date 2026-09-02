"""Which model wrote which document.

Every bullet carries a `from Fxxx:` line so a claim can be traced back to the
fact it came from. Nothing recorded the model that did the rewriting -- and
CLAUDE.md is explicit that weaker models drift more, so "which model wrote
this" is part of the same audit trail.

It became unanswerable on 2026-09-02. `gemini-flash-latest` 503-stormed
through its whole 20/day, `daily` fell back to `gemini-flash-lite-latest` for
16 documents, and a separate `rescreen` re-answered screening on flash-lite
for documents whose bullets flash-latest had written. Afterwards no artifact
could say which was which: the sidecar held job, summary, cover_note,
gap_honesty, bullets, flags and screening, and mtimes had been rewritten by
rescreen anyway.

Tailoring and screening are two calls and can land on two models, so they are
recorded separately rather than as one field.
"""
from pathlib import Path

from jobpipe import render, tailor

FLASH = "gemini-flash-latest"
LITE = "gemini-flash-lite-latest"


def _out():
    return {"summary": "Infrastructure engineer.", "cover_note": "Hello.",
            "gap_honesty": "No production Kubernetes yet.",
            "selected": []}


def _job():
    return {"id": 42, "title": "DevOps Engineer", "company": "Acme",
            "location": "Bangalore", "description": "", "score": 70,
            "score_reason": "good match", "apply_url": "https://x.test/1",
            "url": "https://x.test/1"}


def _bullets():
    return [{"id": "F001", "text": "Administered RHEL fleets.",
             "original": "Administered RHEL fleets"}]


def test_the_sidecar_records_the_model_behind_each_half():
    doc = render.payload(_job(), _out(), _bullets(), {}, {"answers": []},
                         models={"tailor": FLASH, "screening": LITE})
    assert doc["models"] == {"tailor": FLASH, "screening": LITE}


def test_a_split_document_does_not_collapse_to_one_model():
    """The Amgen case: bullets by flash-latest, screening backfilled on lite."""
    doc = render.payload(_job(), _out(), _bullets(), {}, {"answers": []},
                         models={"tailor": FLASH, "screening": LITE})
    assert doc["models"]["tailor"] != doc["models"]["screening"]


def test_screening_model_is_null_when_screening_never_ran():
    doc = render.payload(_job(), _out(), _bullets(), {}, None,
                         models={"tailor": FLASH, "screening": None})
    assert doc["models"]["screening"] is None


def test_payload_without_models_still_carries_the_key():
    """Absent must be a readable {}, not a KeyError for every consumer."""
    doc = render.payload(_job(), _out(), _bullets(), {}, None)
    assert doc["models"] == {}


def test_the_markdown_names_the_model():
    md = tailor._render(_job(), _out(), _bullets(), [], None, {},
                        models={"tailor": FLASH, "screening": FLASH})
    assert FLASH in md


def test_the_markdown_names_BOTH_when_they_differ():
    md = tailor._render(_job(), _out(), _bullets(), [], {"answers": []}, {},
                        models={"tailor": FLASH, "screening": LITE})
    assert FLASH in md and LITE in md


def test_an_old_document_reports_unknown_rather_than_guessing():
    """A document written before this field existed cannot say. Absent is the
    truth; inferring it from an mtime is not -- rescreen rewrites those."""
    doc = render.parse_markdown("# DevOps Engineer\n**Acme** - Bangalore\n",
                                Path("00042_acme.md"))
    assert doc["models"] == {}


def test_the_model_line_survives_a_markdown_round_trip(tmp_path):
    """`cli tex` rewrites the .md from the sidecar. It must not erase this."""
    md = tailor._render(_job(), _out(), _bullets(), [], None, {},
                        models={"tailor": LITE, "screening": None})
    p = tmp_path / "00042_acme.md"
    p.write_text(md)
    again = tailor._render(_job(), _out(), _bullets(), [], None, {},
                           models=render.payload(_job(), _out(), _bullets(), {},
                                                 None,
                                                 models={"tailor": LITE})["models"])
    assert LITE in again
