"""`daily` compiles every prepared document on every run.

Measured 2026-09-02: 45 PDFs recompiled while only 16 .tex files had changed,
so 29 tectonic runs reproduced byte-identical files -- and every PDF's mtime was
reset, which is how you tell at a glance which documents are new.

Two halves, and both are load-bearing. `is_stale` decides what to compile from
the mtime pair; `write_if_changed` is what makes that mtime mean "the document
changed" instead of "the command ran". Without the second, one no-op `cli tex`
invalidates all 45 again and the first half buys nothing.

Neither is tested through `write_tex`/`cmd_tex` on purpose: those render through
`latexdoc`, which reads the user's real `config/facts.yaml`. CI generates config
from the *.example.yaml templates, so a test that reads the private file passes
locally and fails in CI -- this repo has been caught by that twice.
"""
import os

from jobpipe import render


def _touch(path, text, mtime):
    path.write_text(text)
    os.utime(path, (mtime, mtime))


def test_a_missing_pdf_is_stale(tmp_path):
    tex = tmp_path / "00001_acme.tex"
    tex.write_text(r"\documentclass{article}")
    assert render.is_stale(tex) is True


def test_a_pdf_older_than_its_tex_is_stale(tmp_path):
    tex = tmp_path / "00001_acme.tex"
    pdf = tmp_path / "00001_acme.pdf"
    _touch(pdf, "old", 1_000)
    _touch(tex, r"\documentclass{article}", 2_000)
    assert render.is_stale(tex) is True


def test_a_pdf_newer_than_its_tex_is_current(tmp_path):
    """The one the fix exists for: 29 of 45 documents every night."""
    tex = tmp_path / "00001_acme.tex"
    pdf = tmp_path / "00001_acme.pdf"
    _touch(tex, r"\documentclass{article}", 1_000)
    _touch(pdf, "compiled", 2_000)
    assert render.is_stale(tex) is False


def test_an_unreadable_tex_is_stale_not_skipped(tmp_path):
    """Fail toward compiling. A missing PDF is cheap; a silently skipped one is
    the failure this project keeps writing ledger entries about."""
    assert render.is_stale(tmp_path / "nothing.tex") is True


def test_write_if_changed_leaves_identical_content_alone(tmp_path):
    path = tmp_path / "doc.tex"
    _touch(path, "same", 1_000)
    assert render.write_if_changed(path, "same") is False
    assert path.stat().st_mtime == 1_000


def test_write_if_changed_writes_when_the_content_differs(tmp_path):
    path = tmp_path / "doc.tex"
    _touch(path, "old", 1_000)
    assert render.write_if_changed(path, "new") is True
    assert path.read_text() == "new"
    assert path.stat().st_mtime > 1_000


def test_write_if_changed_creates_a_missing_file(tmp_path):
    path = tmp_path / "doc.tex"
    assert render.write_if_changed(path, "first") is True
    assert path.read_text() == "first"


def test_an_unchanged_rerender_does_not_invalidate_the_pdf(tmp_path):
    """The two halves composed -- the property `make tex && make pdf` needs.

    Re-rendering identical .tex content must leave a current PDF current. An
    unconditional write_text() bumps the mtime and forces all 45 recompiles.
    """
    tex = tmp_path / "00001_acme.tex"
    pdf = tmp_path / "00001_acme.pdf"
    _touch(tex, r"\documentclass{article}", 1_000)
    _touch(pdf, "compiled", 2_000)

    render.write_if_changed(tex, r"\documentclass{article}")
    assert render.is_stale(tex) is False

    render.write_if_changed(tex, r"\documentclass{report}")
    assert render.is_stale(tex) is True
