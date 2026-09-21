"""format_phone() only ever touches French 08 numbers.

Two properties are worth pinning: every writing of an 08 number ATP can hand
over ends up as the same national string, and anything else comes back
byte-for-byte identical — the function is a no-op on the rest of the corpus.
"""

import pytest

from src.phone import format_phone

REWRITTEN = [
    # International, as ATP actually writes them (see atp_places).
    "+33 820 33 22 11",
    "+33820332211",
    "+33 8 20 33 22 11",
    "+33 (0)8 20 33 22 11",
    "0033 8 20 33 22 11",
    "0033820332211",
    "tel:+33 820 33 22 11",
    "TEL:+33820332211",
    # The calling code without its plus, which osm2pgsql data does carry.
    "33 820 33 22 11",
    # National writings: already reachable, but regrouped by two.
    "0820332211",
    "0 820 33 22 11",
    "08 20 33 22 11",
    # The national significant number on its own.
    "820332211",
    # Separators ATP and OSM mix in.
    "+33.820.33.22.11",
    "+33-820-33-22-11",
    "08.20.33.22.11",
    "08-20-33-22-11",
    "(+33) 8 20 33 22 11",
    # Non-breaking and narrow non-breaking spaces, which French typography
    # sprinkles into phone numbers.
    "+33 820 33 22 11",
    "08 20 33 22 11",
    # Leading and trailing whitespace.
    "  +33 820 33 22 11\n",
]


@pytest.mark.parametrize("value", REWRITTEN, ids=repr)
def test_every_writing_of_an_08_number_becomes_the_national_one(value: str) -> None:
    assert format_phone(value) == "08 20 33 22 11"


@pytest.mark.parametrize(
    "prefix", ["0800", "0805", "0806", "0810", "0820", "0825", "0836", "0891", "0892", "0899"]
)
def test_the_whole_08_range_is_rewritten(prefix: str) -> None:
    expected = f"{prefix[:2]} {prefix[2:]} 12 34 56"
    assert format_phone(f"+33 {prefix[1:]} 12 34 56") == expected


UNTOUCHED = [
    # Geographic and mobile numbers, in both writings: not our business.
    "+33 1 23 45 67 89",
    "+33 6 12 34 56 78",
    "01 23 45 67 89",
    "06 12 34 56 78",
    "0033 4 78 00 11 22",
    "+33 (0)5 61 00 00 01",
    # Overseas, where 08 does not exist and the calling code is not 33.
    "+590 590 12 34 56",
    "+262 262 12 34 56",
    "+687 41 23 45",
    # Foreign numbers that must survive a French-only rule.
    "+49 30 123456",
    "+32 800 12 345",
    # Short numbers written the only correct way: nothing to fix.
    "3200",
    "3631",
    "1014",
    "118 712",
    # An 08 number that is not French: same digits, different country.
    "+44 800 123 456",
    # Several numbers in one value. Rewriting would silently keep one.
    "+33 820 33 22 11;+33 1 23 45 67 89",
    "0820332211 / 0123456789",
    "0820332211, 0123456789",
    # Extensions and free text: the digits alone are not the number.
    "+33 820 33 22 11 poste 12",
    "0820 33 22 11 (service client)",
    # Wrong lengths around the 08 shape: eight and ten significant digits.
    "+33 820 33 22 1",
    "+33 820 33 22 111",
    # Not a phone number at all.
    "",
    "   ",
    "n/a",
]


@pytest.mark.parametrize("value", UNTOUCHED, ids=repr)
def test_everything_else_comes_back_unchanged(value: str) -> None:
    assert format_phone(value) is value


def test_none_passes_through() -> None:
    assert format_phone(None) is None


def test_rewriting_is_idempotent() -> None:
    once = format_phone("+33 820 33 22 11")
    assert format_phone(once) == once


# Short numbers (3BPQ, 10XY) are outside E.164 — no international form exists.
# A source that formats everything it holds the international way still emits
# "+33 3631", and that prefix has to go: it is not a writing of the number,
# and OSM holds the bare four digits.
SHORT_PREFIXED = [
    ("+33 3631", "3631"),
    ("+333631", "3631"),
    ("0033 3631", "3631"),
    ("00333631", "3631"),
    ("33 3631", "3631"),
    ("tel:+33 3631", "3631"),
    ("+33 36 31", "3631"),
    ("(+33) 3646", "3646"),
    ("+33-3949", "3949"),
    ("+33.1014", "1014"),
    ("+33 3477", "3477"),
]


@pytest.mark.parametrize(("value", "expected"), SHORT_PREFIXED, ids=repr)
def test_a_prefixed_short_number_loses_its_calling_code(value: str, expected: str) -> None:
    assert format_phone(value) == expected


SHORT_UNTOUCHED = [
    # A calling code is not a prefix here: "3300" is a short number in its own
    # right, and reading it as +33 then "00" would destroy it.
    "3300",
    "1033",
    # An overseas calling code in front of a short number is not a writing
    # anyone produces, and 262/590 numbers of that length are not short ones.
    "+262 3631",
    # A foreign short number: the rule is French-only.
    "+49 3631",
    # Not a short number, prefix or not.
    "+33 2631",
    "+33 36312",
    "+33 363",
]


@pytest.mark.parametrize("value", SHORT_UNTOUCHED, ids=repr)
def test_short_number_lookalikes_are_left_alone(value: str) -> None:
    assert format_phone(value) is value


def test_short_rewriting_is_idempotent() -> None:
    once = format_phone("+33 3631")
    assert format_phone(once) is once
