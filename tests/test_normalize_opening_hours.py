"""normalize_opening_hours and merge_opening_hours: what gets written over.

The SQL function decides which opening_hours a changeset replaces; the
Python one decides what the replacement carries. A wrong pair overwrites a
contributor's `PH off` or flattens seasonal hours into one week, so the
cases are written as classes: every spelling of the same week (nothing to
write), what must stay different, what is left out and kept, what is
unreadable and therefore untouched. Last, the invariant that ties the two:
once written, the value compares equal to ATP's — no second changeset.
"""

import pathlib

import psycopg
import pytest

from src.matching import merge_opening_hours

FN = pathlib.Path(__file__).parent.parent / "migrations" / "026_normalize_opening_hours_fn.sql"


@pytest.fixture(scope="module")
def normalize(db_kwargs):
    with psycopg.connect(**db_kwargs) as conn:
        conn.execute(FN.read_text())
        conn.commit()

        def _normalize(value, with_off=False):
            return conn.execute(
                "SELECT normalize_opening_hours(%s, %s)", (value, with_off)
            ).fetchone()[0]

        yield _normalize


@pytest.mark.parametrize(
    "canonical, spellings",
    [
        ("24/7", ["Mo-Su 00:00-24:00", "00:00-24:00", "Mo-Su 00:00-12:30,12:30-24:00",
                  "24/7; PH off", " 24/7 "]),
        ("Mo-Sa 09:00-19:00", ["Mo-Sa 09:00-19:00; Su closed", "Mo-Sa 09:00-19:00; Su off",
                               "Mo-Sa  09:00 - 19:00", "Mo-Sa 09:00-19:00; PH off",
                               "Mo-Sa 09:00-19:00;", "Mo-Sa 09:00-19:00 ; Su off ;"]),
        ("Tu-We,Fr 08:45-12:30,14:00-18:00; Th 08:45-12:30,15:00-18:00",
         ["Tu-We 08:45-12:30,14:00-18:00; Th 08:45-12:30,15:00-18:00; Fr 08:45-12:30,14:00-18:00",
          "Tu,We,Fr 08:45-12:30, 14:00-18:00; Th 08:45-12:30, 15:00-18:00",
          "Tu-Fr 08:45-12:30,14:00-18:00; Th 08:45-12:30,15:00-18:00"]),
        ("Mo 22:00-02:00", ["Mo 22:00-24:00; Tu 00:00-02:00", "Mo 22:00-2:00"]),
        ("Su 22:00-02:00", ["Su 22:00-24:00; Mo 00:00-02:00"]),
        ("Mo-Fr 08:00-12:00,14:00-18:00", ["Mo-Fr 14:00-18:00,08:00-12:00"]),
        ("Mo,Sa-Su 10:00-12:00", ["Sa-Mo 10:00-12:00"]),
        # A comma between rules, `PH` in a day list: read, not refused.
        ("Mo-Fr 10:30-19:30; Sa 09:45-19:30", ["Mo-Fr 10:30-19:30, Sa 09:45-19:30",
                                               "Mo-Fr 10:30-19:30,Sa 09:45-19:30",
                                               "Mo-Fr 10:30-19:30; Sa 09:45-19:30, PH off"]),
        ("Mo-Sa 09:00-21:30", ["Mo-Sa,PH 09:00-21:30", "PH,Mo-Sa 09:00-21:30", "Mo-Sa 09:00-21:30; PH 10:00-12:00"]),
        ("Mo-Sa 09:00-19:00; Su 10:00-12:00", ["Mo-Sa 09:00-19:00; Su,PH 10:00-12:00",
                                               "Mo-Sa 09:00-19:00; PH,Su 10:00-12:00"]),
    ],
)
def test_one_writing_per_week(normalize, canonical, spellings):
    assert normalize(canonical) == canonical
    for spelling in spellings:
        assert normalize(spelling) == canonical, spelling


def test_the_days_declared_closed_are_written_on_request(normalize):
    """The wiki leaves an unmentioned day unknown; what ATP knows closed is
    written `off`. Never compared: a `Su off` alone is not worth a changeset."""
    assert normalize("Mo-Sa 09:00-19:00; Su closed", with_off=True) == "Mo-Sa 09:00-19:00; Su off"
    assert normalize("Mo,We,Fr 09:00-19:00; Tu,Th off; Sa-Su closed", with_off=True) == "Mo,We,Fr 09:00-19:00; Tu,Th,Sa-Su off"
    assert normalize("Mo-Sa 09:00-19:00", with_off=True) == "Mo-Sa 09:00-19:00"
    assert normalize("Mo-Su off", with_off=True) is None
    assert normalize("Mo-Su 00:00-24:00", with_off=True) == "24/7"
    # A later rule reopens a day; a `PH off` is no weekday.
    assert normalize("Mo-Su off; Mo-Fr 09:00-19:00; PH off", with_off=True) == "Mo-Fr 09:00-19:00; Sa-Su off"
    # A midnight split closes nothing: ATP unmarks the day it spills into.
    assert normalize("Fr 22:00-24:00; Sa 00:00-02:00; Su closed", with_off=True) == "Fr 22:00-02:00; Su off"


def test_a_rule_after_a_comma_adds_to_the_days_it_names(normalize):
    """The wiki's own example: Tu-Fr open morning and evening both."""
    assert normalize("Mo-Fr 08:00-12:00, Tu-Sa 18:00-22:00") == "Mo 08:00-12:00; Tu-Fr 08:00-12:00,18:00-22:00; Sa 18:00-22:00"
    assert normalize("Mo-Fr 08:00-12:00; Tu-Sa 18:00-22:00") == "Mo 08:00-12:00; Tu-Sa 18:00-22:00"
    assert normalize("Mo 20:00-24:00, Tu 00:00-02:00") == "Mo 20:00-02:00"


def test_overlapping_ranges_are_one(normalize):
    assert normalize("Mo-Fr 08:00-14:00, Mo-Fr 12:00-18:00") == "Mo-Fr 08:00-18:00"
    assert normalize("Mo-Fr 08:00-18:00, Mo-Fr 12:00-14:00") == "Mo-Fr 08:00-18:00"


def test_an_explicit_open_says_nothing(normalize):
    assert normalize("Mo-Fr 09:00-12:00 open") == "Mo-Fr 09:00-12:00"
    assert normalize("Mo-Fr 09:00-12:00 open; PH off") == "Mo-Fr 09:00-12:00"


def test_a_later_rule_overrides_the_days_it_names(normalize):
    assert normalize("Mo-Sa 07:30-21:00; Fr,Su 07:30-21:30") == "Mo-Th,Sa 07:30-21:00; Fr,Su 07:30-21:30"
    assert normalize("Mo-Sa 09:00-19:00; Th off") == "Mo-We,Fr-Sa 09:00-19:00"


def test_a_full_day_is_not_joined_to_the_next(normalize):
    assert normalize("Mo 00:00-24:00; Tu 00:00-02:00") == "Mo 00:00-24:00; Tu 00:00-02:00"


def test_different_hours_stay_different(normalize):
    assert normalize("Mo-Fr 08:00-18:00") != normalize("Mo-Fr 08:00-19:00")
    assert normalize("Mo-Fr 08:00-18:00") != normalize("Mo-Sa 08:00-18:00")
    assert normalize("Mo-Fr 08:00-18:00") != normalize("Mo-Fr 08:00-12:00,12:00-18:00; Sa 09:00-12:00")


def test_a_week_of_nothing_is_nothing(normalize):
    assert normalize("PH off") is None
    assert normalize("Mo-Su off") is None
    assert normalize("off") is None
    assert normalize("") is None
    assert normalize('"sur rendez-vous"') is None


def test_rules_that_say_nothing_about_the_week_are_left_out(normalize):
    assert normalize("Mo-Fr 09:00-12:00; PH off") == "Mo-Fr 09:00-12:00"
    assert normalize("Mo-Fr 09:00-12:00; PH closed") == "Mo-Fr 09:00-12:00"
    assert normalize('Mo-Fr 09:00-12:00; "sur rendez-vous"') == "Mo-Fr 09:00-12:00"
    assert normalize("Mo-We 09:00-21:00 || Mo-Su 06:00-23:00 open") == "Mo-We 09:00-21:00"


UNREADABLE = [
    "Mo-Fr 09:00-12:00; Jan Su off",
    "Jan-Mar Mo-Fr 09:00-12:00; Apr-Dec Mo-Fr 09:00-18:00",
    "week 1-26 Mo-Fr 09:00-12:00",
    'Mo-Fr 09:00-12:00 "sur rendez-vous"',
    "Mo-Fr 09:00+",
    "Mo-Fr 09:00-sunset",
    "mo-fr 09:00-12:00",
    "Mo-Fr 9h-12h",
    "Mo-Fr 09:00-12:00; Sa[1] 09:00-12:00",
    "Mo-Fr 09:00-12:00; Su 09:00-12:00 unknown",
]


@pytest.mark.parametrize("value", UNREADABLE)
def test_a_week_it_cannot_read_is_nobodys_to_write(normalize, value):
    """Written back after ATP's rules, such a rule would override them (OSM
    reads the last rule that names a day); flattened, seasonal hours would be
    lost. NULL compares to nothing, so the value is never proposed."""
    assert normalize(value) is None


@pytest.mark.parametrize("value", UNREADABLE)
def test_merge_refuses_what_the_comparison_could_not_read(value):
    """The two must agree: what SQL never proposes, Python never writes.
    A drift between the two regexes crashes rather than uploads."""
    with pytest.raises(ValueError):
        merge_opening_hours(value, "Mo-Fr 08:00-19:00")


@pytest.mark.parametrize(
    "old, written",
    [
        ("Mo-Fr 08:00-18:00; Su off; PH off", "Mo-Sa 08:00-19:00; PH off"),
        # The rightmost rule wins: a `PH off` written before the week was
        # overridden by it, and stays so.
        ("PH off; Mo-Fr 08:00-18:00", "PH off; Mo-Sa 08:00-19:00"),
        ("Mo-Fr 08:00-18:00; PH off; Sa 09:00-12:00", "Mo-Sa 08:00-19:00; PH off"),
        ("Mo-Fr 08:00-18:00 open; PH off", "Mo-Sa 08:00-19:00; PH off"),
        ("Mo-Fr 08:00-12:00, Tu-Sa 18:00-22:00; PH off", "Mo-Sa 08:00-19:00; PH off"),
        ('"sur rendez-vous"', 'Mo-Sa 08:00-19:00; "sur rendez-vous"'),
        ('Mo-Fr 08:00-18:00; "sur rendez-vous"', 'Mo-Sa 08:00-19:00; "sur rendez-vous"'),
        ("Mo-Fr 08:00-18:00 || Mo-Su 06:00-23:00 open", "Mo-Sa 08:00-19:00 || Mo-Su 06:00-23:00 open"),
        ("Mo-Fr 08:00-18:00;PH off;", "Mo-Sa 08:00-19:00; PH off"),
        ("24/7", "Mo-Sa 08:00-19:00"),
        ("24/7; PH off", "Mo-Sa 08:00-19:00; PH off"),
        ("Mo-Fr 08:00-18:00", "Mo-Sa 08:00-19:00"),
        ("Mo-Fr 08:00-18:00, Sa 09:00-12:00, PH off", "Mo-Sa 08:00-19:00; PH off"),
        ("Mo-Fr 08:00-18:00; Su,PH 10:00-12:00", "Mo-Sa 08:00-19:00; PH 10:00-12:00"),
        ("Mo-Fr,PH 08:00-18:00", "Mo-Sa 08:00-19:00; PH 08:00-18:00"),
        ("Mo-Fr 08:00-18:00; PH 10:00-12:00", "Mo-Sa 08:00-19:00; PH 10:00-12:00"),
    ],
)
def test_merge_keeps_what_was_left_out(old, written):
    assert merge_opening_hours(old, "Mo-Sa 08:00-19:00") == written


READABLE = [
    "Mo-Fr 08:00-18:00",
    "Mo-Fr 08:00-18:00; Su off; PH off",
    "Mo-Sa 09:00-19:00; Su closed",
    "Tu,We,Fr 08:45-12:30, 14:00-18:00; Th 08:45-12:30, 15:00-18:00",
    "Mo 22:00-24:00; Tu 00:00-02:00",
    'Mo-Fr 08:00-18:00; "sur rendez-vous"',
    "24/7",
    "24/7; PH off",
    "Mo-We 09:00-21:00 || Mo-Su 06:00-23:00 open",
    "Mo-Fr 09:00-12:00; PH closed",
    "Mo-Fr 08:00-18:00, Sa 09:00-12:00, PH off",
    "Mo-Fr 08:00-18:00; Su,PH 10:00-12:00",
    "Mo-Su,PH 09:00-21:30",
    "PH off; Mo-Fr 08:00-18:00",
    "Mo-Fr 08:00-12:00, Tu-Sa 18:00-22:00 open",
]


@pytest.mark.parametrize("old", READABLE)
@pytest.mark.parametrize("atp", ["Mo-Sa 08:00-19:00; Su closed", "Mo-Fr 09:00-24:00; Sa 00:00-01:00", "Mo-Su 00:00-24:00"])
def test_what_is_written_reads_back_as_atps_week(normalize, old, atp):
    """The invariant between the two functions: the value a changeset leaves
    behind compares equal to ATP's, so the next refresh proposes nothing —
    and everything that was left out of the comparison is still there."""
    written = merge_opening_hours(old, normalize(atp, with_off=True))
    assert normalize(written) == normalize(atp)
    assert ("Su off" in written) == ("Su closed" in atp)
    for kept in ("PH off", "PH closed", '"sur rendez-vous"', "|| Mo-Su 06:00-23:00 open"):
        assert (kept in old) == (kept in written), kept
    assert ("PH" in old) == ("PH" in written)
