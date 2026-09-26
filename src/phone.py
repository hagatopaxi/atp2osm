"""The phone key, generated from the country rather than migrated into it.

A phone number is written a dozen ways, and matching two POIs on it is a match
written into OpenStreetMap: the key both sides are reduced to is the national
significant number, the digits left once the international prefix and the
trunk prefix have been removed, in that order.

Only two values depend on the country, and they are constants of the SQL
function rather than parameters: it is IMMUTABLE and used in functional
indexes, so it cannot read anything at call time. That is also why this lives
here instead of in a migration — a new country must cost a configuration file,
never a schema change. The function is (re)generated whenever those values
move, and the indexes built on it are rebuilt with it.
"""

import logging
import re
from functools import lru_cache
from typing import Any, Final

import psycopg
from psycopg import sql

from src.config import get_country
from src.pipeline._matview import signature

logger = logging.getLogger(__name__)

# The calling codes and the trunk prefix come from the country configuration:
# a country answers to several codes — metropolitan France is +33, but Réunion
# is +262, Guadeloupe +590, New Caledonia +687 — and OSM holds those numbers in
# both their international and their national writing. Reading one code only
# would stop the two writings from ever meeting overseas.

# Short numbers (3BPQ "3631", 10XY "1014"). They are outside E.164: no
# international form exists, and they carry no trunk prefix either. A source
# that formats every number it holds the international way still writes
# "+33 3631", so the calling code has to come off for that writing to meet the
# bare one. 118XYZ is six digits and already clears the length guard below.
SHORT_NUMBER: Final = r"(?:3\d{3}|10\d{2})"

# Built on normalize_phone(), so they hold keys computed by whichever
# definition was current when they were built.
PHONE_INDEXES = ("atp_places_phone_norm_idx", "mv_places_phone_norm_idx")

# Arbitrary, only has to be stable: it serialises concurrent installs.
_LOCK_KEY = 8_314_020_251


def normalize_phone_sql(
    calling_codes: tuple[str, ...] | None = None, trunk_prefix: str | None = None
) -> sql.Composed:
    """The CREATE OR REPLACE for this country's phone key.

    The two values come from a configuration file written outside the
    repository, so they go in as SQL literals. They are checked first all the
    same: anything that is not a run of digits is not a calling code, and a
    configuration saying otherwise is refused rather than installed.
    """
    country = get_country()
    calling_codes = country.calling_codes if calling_codes is None else calling_codes
    trunk_prefix = country.trunk_prefix if trunk_prefix is None else trunk_prefix
    if not calling_codes:
        raise ValueError("at least one calling code is required")
    for code in calling_codes:
        if not re.fullmatch(r"\d{1,3}", code):
            raise ValueError(f"calling code must be 1 to 3 digits, got {code!r}")
    if not re.fullmatch(r"\d{0,2}", trunk_prefix):
        raise ValueError(f"trunk_prefix must be 0 to 2 digits, got {trunk_prefix!r}")
    return sql.SQL(r"""
CREATE OR REPLACE FUNCTION normalize_phone(phone TEXT) RETURNS TEXT
LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE AS $fn$
  WITH country AS (
    SELECT ARRAY[{codes}] AS calling_codes, {trunk_prefix} AS trunk_prefix
  ),
  cleaned AS (
    SELECT REGEXP_REPLACE(BTRIM($1), '^tel:', '', 'i') AS value
  ),
  digits AS (
    SELECT value, REGEXP_REPLACE(value, '\D', '', 'g') AS d FROM cleaned
  ),
  refused AS (
    SELECT value, d,
      -- letters: an extension ("poste 12", "ext. 3"), a vanity number or free
      -- text. Keeping the digits would silently shift the key.
      value ~ '[[:alpha:]]'
      -- list separators: "01 23 45 67 89;01 23 45 67 88" would key on the
      -- second number, which neither party displays first.
      OR value ~ '[;,/]'
      OR LENGTH(d) = 0
      -- E.164 caps a real number at 15 digits.
      OR LENGTH(d) > 15 AS refused
    FROM digits
  ),
  -- The country prefix and the trunk prefix are stripped in turn, never as
  -- alternatives: "+33 (0)1 23 45 67 89" carries both, and it is a writing OSM
  -- is full of.
  --
  -- The longest matching calling code wins, so that a country answering to
  -- both +33 and +330-something could not be read the short way first.
  without_country AS (
    SELECT refused, COALESCE((
      SELECT stripped FROM (
        SELECT CASE
          WHEN d LIKE '00' || code || '%'
            THEN SUBSTRING(d FROM 3 + LENGTH(code))
          -- The + is already gone. Guarded on the remaining length so that a
          -- short number starting with a calling code stays whole: '3300' is
          -- not +33 followed by '00'. A short number wearing a calling code
          -- that is not its own is the exception — it is shorter than the
          -- guard allows, and stripping is the only way it meets the bare
          -- writing the other side holds.
          WHEN d LIKE code || '%' AND (
                 LENGTH(d) - LENGTH(code) >= 6
                 OR SUBSTRING(d FROM 1 + LENGTH(code)) ~ '^{short_number}$'
               )
            THEN SUBSTRING(d FROM 1 + LENGTH(code))
        END AS stripped, LENGTH(code) AS code_length
        FROM unnest(calling_codes) AS code
      ) candidates
      WHERE stripped IS NOT NULL
      ORDER BY code_length DESC
      LIMIT 1
    ), d) AS d, trunk_prefix
    FROM refused, country
  )
  SELECT CASE
    WHEN refused THEN NULL
    WHEN trunk_prefix <> '' AND d LIKE trunk_prefix || '%'
      THEN SUBSTRING(d FROM 1 + LENGTH(trunk_prefix))
    ELSE d
  END
  FROM without_country;
$fn$;
""").format(
        codes=sql.SQL(", ").join(sql.Literal(code) for code in calling_codes),
        trunk_prefix=sql.Literal(trunk_prefix),
        short_number=sql.SQL(SHORT_NUMBER),
    )


def ensure_normalize_phone(
    conn: psycopg.Connection[Any],
    calling_codes: tuple[str, ...] | None = None,
    trunk_prefix: str | None = None,
) -> bool:
    """Install the function for this country; rebuild its indexes if it moved.

    Returns whether anything changed. Cheap enough to call on every startup:
    the signature is stamped on the function as a COMMENT and compared first.

    CREATE OR REPLACE does not touch a functional index built on the previous
    definition — its entries stay as they were computed, and the planner reads
    them as if they matched. Hence the REINDEX, which is the whole reason this
    is not a bare execute() at the call site.
    """
    body = normalize_phone_sql(calling_codes, trunk_prefix)
    calling_codes = calling_codes or get_country().calling_codes
    sig = signature(body.as_string(conn))

    with conn.cursor() as cur:
        # Gunicorn starts several workers at once and REINDEX takes an
        # exclusive lock: without this they would queue up and each redo the
        # work the previous one just did. The loser wakes up on a stamped
        # function and returns False.
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        cur.execute("SELECT obj_description(to_regprocedure('normalize_phone(text)'), 'pg_proc')")
        row = cur.fetchone()
        if row and row[0] == sig:
            return False

        cur.execute(body)
        cur.execute(
            sql.SQL("COMMENT ON FUNCTION normalize_phone(text) IS {}").format(sql.Literal(sig))
        )

        cur.execute(
            """SELECT indexname FROM pg_indexes
               WHERE indexname = ANY(%s)
                 AND schemaname = ANY (current_schemas(false))""",
            (list(PHONE_INDEXES),),
        )
        for (index,) in cur.fetchall():
            logger.info("Rebuilding %s on the new phone key", index)
            cur.execute(sql.SQL("REINDEX INDEX {}").format(sql.Identifier(index)))

    conn.commit()
    logger.info("normalize_phone installed for +%s", ", +".join(calling_codes))
    return True


# Special-rate numbers (08 in France) are not reachable from abroad, so the
# international writing OSM would otherwise get is misleading, and the wiki asks
# for the national one. Short numbers have no international form at all (see
# SHORT_NUMBER): a calling code in front of one is a formatting accident.
#
# The shapes below are the French numbering plan; the calling code they strip
# comes from the configuration. It is the first one — the mainland's: a special
# rate or a short number belongs to the mainland plan, and an overseas code in
# front of four digits is not a writing anyone produces (a 262 number of that
# length is a real number, not a short one). A second country with rules of its
# own turns the shapes into configuration too; until then, generalising them
# would mean inventing a syntax for a case nobody has.
@lru_cache(maxsize=4)
def _national_patterns(mainland_code: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    codes = rf"\+{mainland_code}|00{mainland_code}|{mainland_code}"
    return (
        # The calling code and the trunk prefix are both optional and can be
        # written together: "+33 (0)8 20 33 22 11" carries the two.
        re.compile(rf"^(?:{codes})?0?(8\d{{8}})$"),
        re.compile(rf"^(?:{codes})({SHORT_NUMBER})$"),
    )


def format_phone(value: str | None) -> str | None:
    """Rewrite special-rate and short numbers the national way, pass the rest on."""
    if not value:
        return value
    special, short = _national_patterns(get_country().calling_codes[0])
    digits = re.sub(r"[\s.()\u00a0\u202f-]|^tel:", "", value, flags=re.IGNORECASE)
    match = special.match(digits)
    if match:
        n = "0" + match.group(1)
        return " ".join(n[i : i + 2] for i in range(0, 10, 2))
    match = short.match(digits)
    if match:
        return match.group(1)
    return value
