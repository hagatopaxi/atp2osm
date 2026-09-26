"""normalize_phone, exhaustively.

A dozen lines of SQL decide which POIs are considered the same place, and a
wrong match is written into OpenStreetMap. The cases are therefore written as
equivalence classes (everything that must produce the same key) and
discrimination classes (everything that must not), never as a handful of
hand-picked pairs.

Needs the local PostGIS (podman-compose up -d); skipped otherwise. Everything
happens in a throwaway schema.
"""

import pathlib
from collections.abc import Iterator
from typing import Final, LiteralString

import psycopg
import pytest

from src.config import Database
from src.db import sql_file
from src.phone import ensure_normalize_phone, normalize_phone_sql
from tests.conftest import Connection, one

MIGRATIONS = pathlib.Path(__file__).parent.parent / "migrations"
SCHEMA: Final = "test_normalize_phone"
# Each generation of the function in a schema of its own: the CREATE is
# unqualified, so it lands in the first schema of the search_path.
LEGACY: Final = f"{SCHEMA}_legacy"
GERMAN: Final = f"{SCHEMA}_de"
SCHEMAS: Final = (SCHEMA, LEGACY, GERMAN)
LEGACY_FUNCTION: Final = f"{LEGACY}.normalize_phone"
GERMAN_FUNCTION: Final = f"{GERMAN}.normalize_phone"

LEGACY_SQL = MIGRATIONS / "012_normalize_phone_fn.sql"


@pytest.fixture(scope="module")
def conn(test_db: Database) -> Iterator[Connection]:
    with psycopg.connect(test_db.conninfo) as c:
        for schema in SCHEMAS:
            c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            c.execute(f"CREATE SCHEMA {schema}")
        # The function 012 defines: the reference the rewrite must not silently
        # diverge from. Read from the migration rather than copied over, so an
        # edit there breaks this test instead of going unnoticed.
        c.execute(f"SET search_path TO {LEGACY}")
        c.execute(sql_file(LEGACY_SQL))
        # The same generator with German constants. Nothing but the two values
        # changes, which is the whole claim being made about the rewrite.
        c.execute(f"SET search_path TO {GERMAN}")
        c.execute(normalize_phone_sql(("49",), "0"))
        c.execute(f"SET search_path TO {SCHEMA}")
        c.execute(normalize_phone_sql())
        c.commit()
        yield c
        c.rollback()
        for schema in SCHEMAS:
            c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.commit()


def norm(
    conn: Connection, value: str | None, function: LiteralString = "normalize_phone"
) -> str | None:
    return one(conn.execute(f"SELECT {function}(%s)", (value,)).fetchone())[0]


# --- 1. Equivalence -------------------------------------------------------
#
# Every writing of one number, as they actually appear in OSM and ATP. The
# assertion covers the whole class, not selected pairs.

EQUIVALENCE_CLASSES = {
    "123456789": [
        "+33 1 23 45 67 89",
        "+33123456789",
        "0033 1 23 45 67 89",
        "0033123456789",
        "00 33 1 23 45 67 89",
        "01 23 45 67 89",
        "0123456789",
        "01.23.45.67.89",
        "01-23-45-67-89",
        "01 23 45 67 89 ",
        " 01 23 45 67 89",
        "(01) 23 45 67 89",
        "+33 (0)1 23 45 67 89",
        "+33 (0) 1 23 45 67 89",
        "0033 (0)1 23 45 67 89",
        "tel:+33-1-23-45-67-89",
        "TEL:+33 1 23 45 67 89",
        "01\t23\t45\t67\t89",
        "01 23 45 67 89",  # no-break space
        "01 23 45 67 89",  # narrow no-break space
        "+33 1 23 45 67 89",  # thin space
        "01 23 45 67 89",  # collapsed double spaces
    ],
    "612345678": [
        "+33 6 12 34 56 78",
        "06 12 34 56 78",
        "06.12.34.56.78",
        "0033612345678",
        "+33 (0)6 12 34 56 78",
    ],
    # Overseas: France answers to nine calling codes, and OSM holds both
    # writings. Réunion and Mayotte.
    "262300300": [
        "+262 262 30 03 00",
        "0262 30 03 00",
        "0262300300",
        "00262262300300",
        "+262 (0)262 30 03 00",
    ],
    # Guadeloupe.
    "590123456": [
        "+590 590 12 34 56",
        "0590 12 34 56",
        "0590123456",
    ],
    # New Caledonia, where the national number is six digits — the case a
    # fixed-width key would have got wrong.
    "412345": [
        "+687 41 23 45",
        "41 23 45",
        "412345",
    ],
    "800123456": [
        "0 800 123 456",
        "+33 800 123 456",
        "0800123456",
    ],
    # Short numbers have no international form: "+33 3631" is a source
    # formatting everything the same way, not a writing of the number. It has
    # to land on the bare digits OSM holds, or the POI is never matched.
    "3631": [
        "3631",
        "+33 3631",
        "0033 3631",
        "33 3631",
        "tel:+33 3631",
        "+33 36 31",
    ],
    "1014": [
        "1014",
        "+33 1014",
    ],
}


@pytest.mark.parametrize(
    ("expected", "value"),
    [(k, v) for k, values in EQUIVALENCE_CLASSES.items() for v in values],
    ids=[repr(v) for values in EQUIVALENCE_CLASSES.values() for v in values],
)
def test_equivalent_writings_share_one_key(conn: Connection, expected: str, value: str) -> None:
    assert norm(conn, value) == expected


def test_every_class_is_internally_consistent(conn: Connection) -> None:
    for expected, values in EQUIVALENCE_CLASSES.items():
        keys = {norm(conn, v) for v in values}
        assert keys == {expected}, f"class {expected} split into {keys}"


def test_classes_never_overlap(conn: Connection) -> None:
    keys = [norm(conn, values[0]) for values in EQUIVALENCE_CLASSES.values()]
    assert len(set(keys)) == len(keys)


# --- 2. The algorithm carries no French assumption ------------------------

GERMAN_CLASS = [
    "+49 30 123456",
    "0049 30 123456",
    "030 123456",
    "030123456",
    "+49 (0)30 123456",
]


def test_the_same_algorithm_works_for_another_country(conn: Connection) -> None:
    keys = {norm(conn, value, GERMAN_FUNCTION) for value in GERMAN_CLASS}
    assert keys == {"30123456"}


def test_a_german_number_is_left_alone_by_the_french_function(conn: Connection) -> None:
    # One instance serves one country: a foreign number is not something to
    # normalize, only something that will not match. What matters is that it
    # does not collide with a local one.
    assert norm(conn, "+49 30 123456") != norm(conn, "030 123456")


# --- 3. Discrimination ----------------------------------------------------

DISTINCT_NUMBERS = [
    "01 23 45 67 89",
    "01 23 45 67 88",  # last digit
    "02 23 45 67 89",  # first digit
    "01 23 45 67 80",
    "04 23 45 67 89",  # another region
    "01 24 45 67 89",
    "0123456",  # shorter
    "3949",  # short service number
    "118 712",
    "15",
]


def test_distinct_numbers_never_collide(conn: Connection) -> None:
    keys = {value: norm(conn, value) for value in DISTINCT_NUMBERS}
    assert len(set(keys.values())) == len(DISTINCT_NUMBERS), keys


# --- 4. Short forms -------------------------------------------------------


@pytest.mark.parametrize(
    ("short", "long_ending_the_same"),
    [
        ("3949", "01 23 45 39 49"),
        ("118 712", "01 23 45 87 12"),
        ("15", "01 23 45 67 15"),
        ("112", "01 23 45 61 12"),
        ("3310", "+33 1 23 45 33 10"),
        # "3300" is a short number, not +33 followed by "00".
        ("3300", "+33 1 23 45 33 00"),
    ],
)
def test_short_number_does_not_collide_with_a_long_one(
    conn: Connection, short: str, long_ending_the_same: str
) -> None:
    assert norm(conn, short) != norm(conn, long_ending_the_same)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("3949", "3949"),
        ("15", "15"),
        ("118 712", "118712"),
        ("3310", "3310"),
        ("3300", "3300"),
        ("1033", "1033"),
    ],
)
def test_short_numbers_keep_all_their_digits(
    conn: Connection, value: str, expected: str | None
) -> None:
    # Neither the calling code nor the trunk prefix may bite into a number too
    # short to carry one.
    assert norm(conn, value) == expected


# --- 5. Refusals ----------------------------------------------------------
#
# Anything that is not a single phone number must produce NULL rather than a
# partial key: NULL never equals anything, so the row stops being matchable by
# phone instead of matching the wrong place.

REFUSED = [
    # several numbers in one value
    "01 23 45 67 89;01 23 45 67 88",
    "01 23 45 67 89; 01 23 45 67 88",
    "+33 1 23 45 67 89;+33 1 23 45 67 88",
    "01 23 45 67 89, 01 23 45 67 88",
    "01 23 45 67 89 / 01 23 45 67 88",
    # short enough to pass the 15-digit cap: only the separator says these are
    # two numbers, which is what makes them the cases that matter
    "3949;3950",
    "01 23 45 67 89/15",
    "15,112",
    # extensions
    "+33 1 23 45 67 89 poste 12",
    "01 23 45 67 89 ext. 3",
    "01 23 45 67 89 x12",
    # not a number at all
    "",
    " ",
    " ",
    "sur rendez-vous",
    "0800-GO-OSM",
    "0 800 GO OSM",
    "-",
    "()",
    "+",
    # too long to be E.164
    "1234567890123456",
    "+33 1234 5678 9012 345",
]


@pytest.mark.parametrize("value", REFUSED, ids=[repr(v) for v in REFUSED])
def test_refused_values_yield_null(conn: Connection, value: str) -> None:
    assert norm(conn, value) is None


def test_fifteen_digits_are_still_accepted(conn: Connection) -> None:
    assert norm(conn, "123456789012345") is not None


# --- 6. NULL and the catalog ---------------------------------------------


def test_null_in_null_out(conn: Connection) -> None:
    assert norm(conn, None) is None


def test_function_attributes_are_preserved(conn: Connection) -> None:
    # A lost attribute breaks the functional indexes without breaking a single
    # behavioural test, so it is asserted on the catalog itself.
    row = conn.execute(
        """SELECT provolatile, proisstrict, proparallel
           FROM pg_proc WHERE proname = 'normalize_phone'
             AND pronamespace = %s::regnamespace""",
        (SCHEMA,),
    ).fetchone()
    assert row == ("i", True, "s")


# --- 7. No regression against the function being replaced -----------------
#
# On plainly written French numbers, the old and the new function must split
# the corpus into the very same groups. Different keys are fine — what must
# not change is which numbers are considered equal.

PLAIN_FRENCH_CORPUS = [
    "+33 1 23 45 67 89",
    "0033 1 23 45 67 89",
    "01 23 45 67 89",
    "01.23.45.67.89",
    "01-23-45-67-89",
    "0123456789",
    "+33 6 12 34 56 78",
    "06 12 34 56 78",
    "0612345678",
    "+33 4 78 00 11 22",
    "04 78 00 11 22",
    "04.78.00.11.22",
    "+33 5 61 00 00 01",
    "05 61 00 00 01",
    "3949",
    "118 712",
]


def _partition(conn: Connection, corpus: list[str], function: LiteralString) -> set[frozenset[str]]:
    groups: dict[str | None, set[str]] = {}
    for value in corpus:
        groups.setdefault(norm(conn, value, function), set()).add(value)
    return {frozenset(v) for v in groups.values()}


def test_partition_is_identical_to_the_legacy_function(conn: Connection) -> None:
    assert _partition(conn, PLAIN_FRENCH_CORPUS, "normalize_phone") == _partition(
        conn, PLAIN_FRENCH_CORPUS, LEGACY_FUNCTION
    )


# The divergences, listed one by one. Each is a case the legacy function got
# wrong and the new one gets right; nothing outside this list may diverge.
DIVERGENCES = [
    # the legacy function kept "(0)" and made its own group out of it
    ("+33 (0)1 23 45 67 89", "01 23 45 67 89"),
    # it did not know the tel: scheme
    ("tel:+33 1 23 45 67 89", "01 23 45 67 89"),
    # its separator class was ASCII only
    ("01 23 45 67 89", "01 23 45 67 89"),
    ("01 23 45 67 89", "01 23 45 67 89"),
    # it refused to strip a calling code from anything shorter than six
    # remaining digits, which left a prefixed short number in a group of its own
    ("+33 3631", "3631"),
]


@pytest.mark.parametrize(("odd", "plain"), DIVERGENCES, ids=[repr(a) for a, _ in DIVERGENCES])
def test_listed_divergences_are_fixes(conn: Connection, odd: str, plain: str) -> None:
    assert norm(conn, odd, LEGACY_FUNCTION) != norm(conn, plain, LEGACY_FUNCTION)
    assert norm(conn, odd) == norm(conn, plain)


MANGLED_BY_LEGACY = [
    "01 23 45 67 89;01 23 45 67 88",
    "+33 1 23 45 67 89 poste 12",
    "sur rendez-vous",
]


@pytest.mark.parametrize("value", MANGLED_BY_LEGACY, ids=[repr(v) for v in MANGLED_BY_LEGACY])
def test_values_the_legacy_function_mangled_are_now_refused(conn: Connection, value: str) -> None:
    assert norm(conn, value, LEGACY_FUNCTION) is not None
    assert norm(conn, value) is None


# --- 8. The index must agree with the function ----------------------------


def test_functional_index_agrees_with_the_function(conn: Connection) -> None:
    """A functional index built before the rewrite has to be rebuilt by it.

    Without the REINDEX in the migration the index keeps the keys computed by
    the old definition, and an index scan silently disagrees with a sequential
    one.
    """
    conn.execute("DROP TABLE IF EXISTS phones")
    conn.execute("CREATE TABLE phones (phone TEXT)")
    conn.execute(
        "INSERT INTO phones SELECT unnest(%s::text[])",
        ([*PLAIN_FRENCH_CORPUS, "+33 (0)1 23 45 67 89"],),
    )
    conn.execute("CREATE INDEX phones_norm_idx ON phones (normalize_phone(phone))")
    conn.execute("ANALYZE phones")

    target = norm(conn, "01 23 45 67 89")

    conn.execute("SET LOCAL enable_seqscan = off")
    with_index = one(
        conn.execute(
            "SELECT count(*) FROM phones WHERE normalize_phone(phone) = %s", (target,)
        ).fetchone()
    )[0]

    conn.execute("SET LOCAL enable_seqscan = on")
    conn.execute("SET LOCAL enable_indexscan = off")
    conn.execute("SET LOCAL enable_bitmapscan = off")
    without_index = one(
        conn.execute(
            "SELECT count(*) FROM phones WHERE normalize_phone(phone) = %s", (target,)
        ).fetchone()
    )[0]
    conn.execute("RESET enable_indexscan")
    conn.execute("RESET enable_bitmapscan")

    assert with_index == without_index > 0


# --- 9. Installing it, and what happens when the country changes ----------


@pytest.fixture
def install_schema(conn: Connection) -> Iterator[None]:
    conn.execute("DROP SCHEMA IF EXISTS install_check CASCADE")
    conn.execute("CREATE SCHEMA install_check")
    conn.execute("SET search_path TO install_check")
    conn.commit()
    yield
    conn.rollback()
    conn.execute("DROP SCHEMA IF EXISTS install_check CASCADE")
    conn.execute(f"SET search_path TO {SCHEMA}")
    conn.commit()


def test_install_is_idempotent(conn: Connection, install_schema: None) -> None:
    assert ensure_normalize_phone(conn) is True
    assert ensure_normalize_phone(conn) is False


def test_changing_the_country_reinstalls_the_function(
    conn: Connection, install_schema: None
) -> None:
    ensure_normalize_phone(conn)
    assert norm(conn, "+49 30 123456") != "30123456"

    assert ensure_normalize_phone(conn, ("49",), "0") is True
    assert norm(conn, "+49 30 123456") == "30123456"


def test_changing_the_country_rebuilds_the_index(conn: Connection, install_schema: None) -> None:
    """The whole reason install is not a bare CREATE OR REPLACE.

    A functional index keeps the keys computed by the definition in force when
    it was built; without the rebuild an index scan and a sequential scan
    disagree, silently.
    """
    ensure_normalize_phone(conn)
    conn.execute("CREATE TABLE atp_places (phone TEXT)")
    conn.execute(
        "INSERT INTO atp_places SELECT unnest(%s::text[])",
        (["+49 30 123456", "030 123456", "+33 1 23 45 67 89"],),
    )
    conn.execute("CREATE INDEX atp_places_phone_norm_idx ON atp_places (normalize_phone(phone))")
    conn.execute("ANALYZE atp_places")
    conn.commit()

    ensure_normalize_phone(conn, ("49",), "0")
    conn.execute("ANALYZE atp_places")

    conn.execute("SET LOCAL enable_seqscan = off")
    with_index = one(
        conn.execute(
            "SELECT count(*) FROM atp_places WHERE normalize_phone(phone) = '30123456'"
        ).fetchone()
    )[0]
    conn.execute("SET LOCAL enable_seqscan = on")
    conn.execute("SET LOCAL enable_indexscan = off")
    conn.execute("SET LOCAL enable_bitmapscan = off")
    without_index = one(
        conn.execute(
            "SELECT count(*) FROM atp_places WHERE normalize_phone(phone) = '30123456'"
        ).fetchone()
    )[0]
    conn.execute("RESET enable_indexscan")
    conn.execute("RESET enable_bitmapscan")

    assert with_index == without_index == 2


@pytest.mark.parametrize(
    ("calling_codes", "trunk_prefix"),
    [
        ((), "0"),
        (("",), "0"),
        (("3a",), "0"),
        (("3333",), "0"),
        (("33", "26a"), "0"),
        (("33",), "x"),
        (("33",), "000"),
    ],
)
def test_a_nonsensical_country_is_refused(
    calling_codes: tuple[str, ...], trunk_prefix: str
) -> None:
    # The values come from a file written outside the repository.
    with pytest.raises(ValueError, match=r"digits|calling code"):
        normalize_phone_sql(calling_codes, trunk_prefix)


def test_a_longer_calling_code_wins_over_a_shorter_one(conn: Connection) -> None:
    # +262 must not be read as +26 or +2 by an unlucky ordering.
    assert norm(conn, "+262 262 30 03 00") == norm(conn, "0262 30 03 00")


def test_two_installs_at_once_do_the_work_once(
    conn: Connection, install_schema: None, test_db: Database
) -> None:
    """Gunicorn starts several workers; only one may rebuild the indexes."""
    other = psycopg.connect(test_db.conninfo)
    with other:
        other.execute("SET search_path TO install_check")
        assert ensure_normalize_phone(conn) is True
        assert ensure_normalize_phone(other) is False
