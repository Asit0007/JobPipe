"""The resume page limit is a config value, not a constant in cli.py.

Asit set the body to 14pt on 2026-09-08 (b902ef9) and confirmed it on
2026-09-13. That took every document to three pages, so the old "warn past two"
fired on 101 of 101 -- and a warning with no negative case is decoration, not a
signal. The threshold moved; the font did not.
"""
import pathlib

import yaml

EXAMPLE = pathlib.Path(__file__).resolve().parents[1] / "config" / "profile.example.yaml"


def _thresholds():
    # The example, never the user's real config: CI generates config from the
    # templates, so a test that reads config/profile.yaml passes locally and
    # fails in CI for reasons unrelated to the code.
    return yaml.safe_load(EXAMPLE.read_text())["thresholds"]


def test_the_example_declares_a_page_limit():
    assert "max_resume_pages" in _thresholds(), \
        "cli pdf reads this; without it the limit silently falls back to a constant"


def test_the_limit_matches_the_14pt_shape():
    assert _thresholds()["max_resume_pages"] == 3


def test_the_limit_is_a_positive_int():
    v = _thresholds()["max_resume_pages"]
    assert isinstance(v, int) and v >= 1


def test_the_cooldown_window_is_declared_too():
    """Same contract: cooldown.py reads it with a default, so a missing key is
    silent rather than loud."""
    assert _thresholds().get("company_cooldown_days") == 90
