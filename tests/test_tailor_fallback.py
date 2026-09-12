"""Bug 7.49: the tailor fallback fired only on TOTAL failure, never partial.

Both of the last two `daily` runs that reached `prepare` hit this. The primary
model returned a few documents and then 503'd for the rest, so `written == 0`
was False and the fallback -- which had budget for dozens more -- never ran.
"""
import pytest

from jobpipe.cli import fallback_room


def test_a_PARTIAL_failure_falls_back():
    """The regression. 2026-09-12: asked 10, wrote 1, 86 flash-lite calls idle."""
    assert fallback_room(written=1, asked=10, fb_left=86, target=15) == 14


def test_the_2026_09_05_shape_too():
    """Asked 10, wrote 2, and the fallback sat out that run as well."""
    assert fallback_room(written=2, asked=10, fb_left=200, target=15) > 0


def test_total_failure_still_falls_back_exactly_as_it_used_to():
    """`written < asked` subsumes the zero case -- nothing stops triggering.

    The old code computed min(fb_left // 2, 15); this must still match it.
    """
    assert fallback_room(written=0, asked=10, fb_left=86, target=15) == 15
    assert fallback_room(written=0, asked=10, fb_left=10, target=15) == 5


def test_full_success_does_not_fall_back():
    assert fallback_room(written=10, asked=10, fb_left=500, target=15) == 0


def test_more_written_than_asked_does_not_fall_back():
    """Defensive: a count that overshoots must not wrap into a huge room."""
    assert fallback_room(written=12, asked=10, fb_left=500, target=15) == 0


def test_the_room_is_the_SHORTFALL_not_the_whole_target():
    """A partial success tops the day up; it does not restart the count.

    Asking for the full target after writing 9 would queue 24 documents on a
    15/day cap -- work nobody looks at, spent from the scarcest budget there is.
    """
    assert fallback_room(written=9, asked=10, fb_left=500, target=15) == 6


def test_no_fallback_budget_means_no_attempt():
    assert fallback_room(written=1, asked=10, fb_left=1, target=15) == 0
    assert fallback_room(written=1, asked=10, fb_left=0, target=15) == 0


@pytest.mark.parametrize("written", range(0, 16))
def test_room_never_pushes_the_day_past_its_target(written):
    room = fallback_room(written=written, asked=99, fb_left=10_000, target=15)
    assert written + room <= 15
