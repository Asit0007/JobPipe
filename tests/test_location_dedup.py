"""One requisition, many boards, many spellings of the same city.

Measured 2026-09-12 on the live corpus: 30 groups among the 502 rows that had
reached the queue were the same job twice, differing only in how the board
wrote the location. One of them was already `applied` while its twin sat
`shortlisted`, which would have spent scarce tailor budget on a duplicate.
"""
import pytest

from jobpipe.normalize import canon_place, fingerprint, is_near_duplicate

FP = lambda loc: fingerprint("Heaptrace", "DevOps Engineer", loc)  # noqa: E731


@pytest.mark.parametrize("a,b", [
    ("Bengaluru", "Bengaluru, Karnataka"),      # Glassdoor vs Indeed, measured
    ("Pune", "Pune, Maharashtra"),
    ("Pune", "Pune Division"),
    ("Hyderabad, Telangana", "Hyderābād"),      # the macron, measured on MetLife
    ("Bangalore", "Bengaluru"),                 # alias
    ("Gurgaon", "Gurugram"),
    ("Mumbai", "Bombay"),
])
def test_the_same_city_written_differently_is_one_row(a, b):
    assert canon_place(a) == canon_place(b)
    assert FP(a) == FP(b)


@pytest.mark.parametrize("a,b", [
    ("San Francisco", "San Diego"),   # both lead with "san" -- _city() would merge these
    ("New York", "New Delhi"),        # and both with "new"
    ("Pune", "Mumbai"),
    ("India", "Pune"),                # nationwide is not the Pune posting
    ("Toronto", "New York"),          # 7.4, in the layer above
])
def test_different_places_stay_different_rows(a, b):
    assert FP(a) != FP(b)


def test_an_unknown_city_is_left_alone_rather_than_guessed():
    """The list cannot mis-collapse what it does not recognise."""
    assert canon_place("Katowice, Silesia") == "katowice silesia"
    assert FP("Katowice, Silesia") != FP("Katowice")


def test_canon_place_is_what_the_fingerprint_uses():
    """Regression guard for the actual bug: fingerprint() ignored the helper.

    `_city()` existed and its docstring described this exact problem, while
    fingerprint() went on hashing the raw canonical string. A second definition
    of "same place" that nothing consults is how this survived.
    """
    assert FP("Bengaluru, Karnataka") == FP("bengaluru")


def test_the_near_duplicate_gate_still_refuses_to_cross_cities():
    """canon_place must not have loosened is_near_duplicate (7.4)."""
    a = {"company": "Acme", "title": "Senior SRE", "location": "Toronto"}
    b = {"company": "Acme", "title": "SRE", "location": "New York"}
    assert not is_near_duplicate(a, b)
    same_city = {"company": "Acme", "title": "SRE", "location": "Toronto"}
    assert is_near_duplicate(a, same_city)
