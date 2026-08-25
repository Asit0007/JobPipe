"""facts.yaml is validated on load and fails loudly (CLAUDE.md 7.16).

A typo'd ID or a missing `verified` key used to drop a fact silently, which is
indistinguishable from the model choosing not to use it.
"""
import pytest

from jobpipe.config import ConfigError, _validate_facts


def _cfg(*facts):
    return {"roles": [{"company": "X", "facts": list(facts)}], "projects": []}


def test_a_valid_file_passes():
    _validate_facts(_cfg({"id": "F001", "verified": True, "text": "did a thing"}))


@pytest.mark.parametrize("bad, expect", [
    ({"id": "f1", "verified": True, "text": "t"},        "must match"),
    ({"id": "F001", "text": "t"},                        "missing 'verified'"),
    ({"id": "F001", "verified": "yes", "text": "t"},     "must be true or false"),
    ({"id": "F001", "verified": True, "text": "  "},     "is empty"),
    ({"verified": True, "text": "t"},                    "missing 'id'"),
])
def test_malformed_facts_raise_rather_than_vanish(bad, expect):
    with pytest.raises(ConfigError, match=expect):
        _validate_facts(_cfg(bad))


def test_duplicate_ids_are_caught():
    with pytest.raises(ConfigError, match="duplicate id"):
        _validate_facts(_cfg({"id": "F001", "verified": True, "text": "a"},
                             {"id": "F001", "verified": True, "text": "b"}))


def test_every_problem_is_reported_not_just_the_first():
    with pytest.raises(ConfigError) as e:
        _validate_facts(_cfg({"id": "f1", "verified": True, "text": "a"},
                             {"id": "F002", "text": "b"}))
    assert "2 problem(s)" in str(e.value)
