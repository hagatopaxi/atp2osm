import random
import re

from psycopg import Cursor
from psycopg.rows import dict_row
from typing import Any, NamedTuple

from src.config import get_country
from src.phone import format_phone


# The one ATP <-> OSM matching query, shared by /validate (get_filtered) and by
# the mv_places_brand materialized view that feeds the counter on the brand
# list. Both MUST stay on the same SQL: two diverging copies are what once made
# the list show 50 POIs and /validate 60.
#
# Deduplication includes atp_brand_wikidata: a single OSM object can match two
# different brands, and must then be counted for each one (like /validate does,
# which filters on a single brand).
#
# `matched_poi_sql()` fills the radius, which the country sets: 500 m is
# calibrated on European urban density, and a country whose POIs sit further
# apart says so in its configuration rather than in this file.
MATCHED_POI_SQL = """
    WITH joined_poi AS (
    SELECT
        *,
        osm.tags as old_tags,
        ST_X(ST_Centroid(osm.geom)) AS lon,
        ST_Y(ST_Centroid(osm.geom)) AS lat,
        -- Written the way OSM writes it (normalize_opening_hours), the days
        -- ATP knows closed included: what is compared below is what gets
        -- written, minus those.
        normalize_opening_hours(atp.opening_hours, true) AS atp_opening_hours,
        atp.phone as atp_phone,
        atp.email as atp_email,
        atp.website as atp_website,
        atp.country as atp_country,
        atp.city as atp_city,
        atp.source_uri as atp_source_uri,
        atp.brand as atp_brand,
        atp.brand_wikidata as atp_brand_wikidata,
        (
            (atp.opening_hours IS NOT NULL AND osm.opening_hours IS NULL)
            OR (atp.email   IS NOT NULL AND osm.email   IS NULL)
            OR (atp.phone   IS NOT NULL AND osm.phone   IS NULL)
            OR (atp.website IS NOT NULL AND osm.website IS NULL)
            -- NSI completes objects too: an object only NSI has something to
            -- say about must count in the batches, otherwise apply_on_node
            -- would produce a change the brand list never announced.
            OR EXISTS (
                SELECT 1 FROM jsonb_object_keys(COALESCE(osm.nsi_tags, '{{}}'::jsonb)) AS k
                WHERE NOT osm.tags ? k
            )
        ) AS is_importable,
        -- Wave 2: the tags whose value ATP would replace, key -> ATP value.
        -- Computed here rather than in Python so that a single expression
        -- decides both the count on the brand list and the diff /validate
        -- shows — and so that the phone comparison keeps using
        -- normalize_phone(), which only exists in SQL.
        --
        -- Read off osm.tags rather than off the normalized columns: those
        -- COALESCE `phone` over `contact:phone`, so an object carrying both,
        -- with only the contact: one out of date, would look up to date. Both
        -- writings are compared, and a difference on either makes the tag
        -- modifiable — apply_on_node then rewrites every variant that is
        -- there. An absent writing compares to nothing and weighs nothing:
        -- filling a hole is wave 1's business.
        (
            SELECT COALESCE(jsonb_object_agg(t.key, t.atp_value), '{{}}'::jsonb)
            FROM (VALUES
                -- Compared on the weekday rules alone, both sides in the
                -- same writing: spaces, `closed`/`off`, `24/7`, day lists
                -- are spellings, not differences. What ATP never scrapes —
                -- `PH off`, comments — is neither compared nor lost:
                -- merge_opening_hours writes it back. A value with no
                -- readable week (NULL: seasonal, commented, lowercase…) is
                -- left to humans, never overwritten.
                ('opening_hours', normalize_opening_hours(atp.opening_hours, true),
                    normalize_opening_hours(osm.tags->>'opening_hours')
                        <> normalize_opening_hours(atp.opening_hours)),
                ('email', atp.email,
                    LOWER(osm.tags->>'email') <> LOWER(atp.email)
                    OR LOWER(osm.tags->>'contact:email') <> LOWER(atp.email)),
                ('phone', atp.phone,
                    normalize_phone(osm.tags->>'phone') <> normalize_phone(atp.phone)
                    OR normalize_phone(osm.tags->>'contact:phone')
                        <> normalize_phone(atp.phone)),
                ('website', atp.website,
                    LOWER(REGEXP_REPLACE(osm.tags->>'website', '^https?://', '', 'i'))
                        <> LOWER(REGEXP_REPLACE(atp.website, '^https?://', '', 'i'))
                    OR LOWER(REGEXP_REPLACE(osm.tags->>'contact:website', '^https?://', '', 'i'))
                        <> LOWER(REGEXP_REPLACE(atp.website, '^https?://', '', 'i')))
            ) AS t(key, atp_value, differs)
            WHERE t.atp_value IS NOT NULL AND t.differs
        ) AS modifiable_tags,
        ST_Distance(osm.geom::geography, ST_GeomFromGeoJSON(atp.geom)::geography) AS atp_distance,
        count(*) FILTER (WHERE osm.node_type = 'node')                 OVER (PARTITION BY atp.id) AS pt_cnt,
        count(*) FILTER (WHERE osm.node_type IN ('way', 'relation'))   OVER (PARTITION BY atp.id) AS poly_cnt
    FROM
        mv_places osm
    INNER JOIN atp_places atp ON
        ST_DWithin(
            osm.geom::geography,
            ST_GeomFromGeoJSON(atp.geom)::geography,
            {match_radius_m}
        )
    WHERE
        {where_options} AND
        (
            osm.brand_wikidata = atp.brand_wikidata
            OR LOWER(osm.brand) = LOWER(atp.brand)
            OR LOWER(osm.name) = LOWER(atp."name")
            OR LOWER(osm.email) = LOWER(atp.email)
            OR LOWER(REGEXP_REPLACE(osm.website, '^https?://', '', 'i')) = LOWER(REGEXP_REPLACE(atp.website, '^https?://', '', 'i'))
            OR normalize_phone(osm.phone) = normalize_phone(atp.phone)
        )
    )
    SELECT DISTINCT ON (osm_id, node_type, atp_brand_wikidata)
        *, modifiable_tags <> '{{}}'::jsonb AS is_modifiable
    FROM joined_poi
    WHERE pt_cnt <= 1 AND poly_cnt <= 1
    ORDER BY osm_id, node_type, atp_brand_wikidata, atp_distance
"""


def matched_poi_sql(where_options: str = "TRUE") -> str:
    """The matching query, ready to run: its filters and the country's radius.

    One `format` call, never two: the SQL escapes its own braces (`'{{}}'::jsonb`)
    and a second pass would unescape them into replacement fields.
    """
    return MATCHED_POI_SQL.format(
        where_options=where_options, match_radius_m=get_country().match_radius_m
    )


class Wave(NamedTuple):
    """One integration typology of a brand.

    Adding wave 3 is adding an entry: nothing else knows how many there are.
    `flag` is the boolean MATCHED_POI_SQL computes for it, and the only thing
    tying a wave to the matches it covers.
    """

    number: int
    flag: str
    batch_size: int
    # Floor of POIs reviewed per batch — the sample may exceed it to cover
    # every changed tag. A batch smaller than that is reviewed in full.
    sample_size: int
    alpha: bool


# Ordered: a brand is on the first wave that still has integrable matches. The
# labels are not here — a module constant is read before any request, so it has
# no locale to resolve against; the templates hold them, keyed by number.
WAVES = (
    # Adding tags an existing POI does not carry.
    Wave(number=1, flag="is_importable", batch_size=100, sample_size=3, alpha=False),
    # Replacing values an existing POI already carries. Small batches while in
    # alpha, the time it takes to see what the community makes of it — and
    # reviewed in full, so a contributor sees every value that is overwritten.
    Wave(number=2, flag="is_modifiable", batch_size=10, sample_size=10, alpha=True),
)

WAVES_BY_NUMBER = {wave.number: wave for wave in WAVES}


def waves_lateral_sql() -> str:
    """`VALUES (number, flag)` for every wave — how mv_places_brand fans a
    match out over the waves it belongs to. A POI can be on two: adding a
    missing phone and replacing a stale website are two integrations."""
    return ", ".join(f"({wave.number}, {wave.flag})" for wave in WAVES)


def get_filtered(
    cursor: Cursor,
    brand: str = None,
    postcode: str = None,
    subdivision_code: str = None,
) -> Cursor:
    options = []
    params = []
    if brand:
        options.append("atp.brand_wikidata = %s")
        params.append(brand)
    if postcode:
        options.append("atp.postcode = %s")
        params.append(postcode)
    if subdivision_code:
        options.append("atp.subdivision_code = %s")
        params.append(subdivision_code)

    query = matched_poi_sql(" AND ".join(options) or "TRUE")

    return cursor.execute(query, params)


# Cooldowns: how long an import keeps hiding what it just touched, until the
# daily refresh drops the integrated POIs from the matches.
SUCCESS_COOLDOWN = "3 months"
ERROR_COOLDOWN = "4 weeks"

# Cooldowns are code constants, never values coming from a request: splicing
# them into the SQL below cannot inject anything. The format is checked at import
# time so that it stays that way.
assert all(
    re.fullmatch(r"\d+ (days|weeks|months)", cooldown)
    for cooldown in (SUCCESS_COOLDOWN, ERROR_COOLDOWN)
)


def _within(cooldown: str) -> str:
    """SQL condition: the import is still within its cooldown."""
    return f"ih.import_date > NOW() - INTERVAL '{cooldown}'"


# Subdivisions still under cooldown, one row per (brand, subdivision). Shared
# between get_all() (the list count) and get_blocked_subdivisions() (batch
# composition): both must block exactly the same ones.
#
# `wave` travels with the row: a cooldown belongs to the typology that earned
# it. A subdivision whose missing tags were added in wave 1 is not thereby
# blocked from having its existing ones reviewed in wave 2.
BLOCKED_DEPARTEMENTS_SQL = f"""
    SELECT ih.brand_wikidata, ih.wave, sub.subdivision_code
    FROM import_subdivisions sub
    JOIN import_history ih ON ih.id = sub.import_id
    WHERE (sub.status IN ('error_osm_api','error_unknown') AND {_within(ERROR_COOLDOWN)})
       OR (sub.status = 'success'                          AND {_within(SUCCESS_COOLDOWN)})
"""

# Imports with no changeset at all: a cancellation, a brand with nothing left to
# integrate, or a pre-migration row the backfill could not detail. They point at
# no subdivision in particular, so they hide the whole brand for the cooldown.
#
# `partial` is absent: it implies some subdivisions succeeded and others failed,
# hence child rows. The backfill (migration 016) detailed them all, and no import
# produces a childless one any more.
BLOCKED_BRANDS_SQL = f"""
    SELECT ih.*
    FROM import_history ih
    WHERE NOT EXISTS (SELECT 1 FROM import_subdivisions sub WHERE sub.import_id = ih.id)
      AND (
        (ih.status IN ('cancelled', 'error') AND {_within(ERROR_COOLDOWN)})
        OR (ih.status = 'success'            AND {_within(SUCCESS_COOLDOWN)})
      )
"""


# Per (brand, wave), what is left to integrate once the cooldowns have had
# their say. get_all() reads the brand's first unfinished wave off it, and
# current_wave() one brand's — the same rows, so the list and /validate can
# never disagree on which wave a brand is on.
UNBLOCKED_WAVES_SQL = f"""
    WITH blocked AS (
        SELECT brand_wikidata, wave, ARRAY_AGG(DISTINCT subdivision_code) AS subs
        FROM ({BLOCKED_DEPARTEMENTS_SQL}) b
        GROUP BY brand_wikidata, wave
    )
    SELECT
        MAX(mvb.brand)     AS brand,
        mvb.brand_wikidata AS brand_wikidata,
        mvb.wave           AS wave,
        SUM(mvb.total)     AS total
    FROM mv_places_brand mvb
    LEFT JOIN blocked ON blocked.brand_wikidata = mvb.brand_wikidata
                     AND blocked.wave = mvb.wave
    WHERE (mvb.brand IS NOT NULL AND mvb.brand_wikidata IS NOT NULL)
      AND NOT (COALESCE(mvb.subdivision_code, '') = ANY(COALESCE(blocked.subs, '{{}}')))
      AND NOT EXISTS (
          SELECT 1 FROM ({BLOCKED_BRANDS_SQL}) blocked_brands
          WHERE blocked_brands.brand_wikidata = mvb.brand_wikidata
            AND blocked_brands.wave = mvb.wave
      )
    GROUP BY mvb.brand_wikidata, mvb.wave
"""


def get_all(osmdb):
    # `total` is the number of POIs *left to integrate*, on the brand's current
    # wave — the first one that still has anything.
    query = f"""
        WITH per_wave AS ({UNBLOCKED_WAVES_SQL}),
        current AS (
            SELECT DISTINCT ON (brand_wikidata) *
            FROM per_wave
            ORDER BY brand_wikidata, wave
        )
        SELECT
            current.brand,
            current.brand_wikidata,
            current.wave,
            current.total,
            ih.last_import,
            ih.last_status
        FROM current
        LEFT JOIN (
            SELECT DISTINCT ON (brand_wikidata)
                brand_wikidata,
                import_date AS last_import,
                status      AS last_status
            FROM import_history
            ORDER BY brand_wikidata, import_date DESC
        ) ih ON ih.brand_wikidata = current.brand_wikidata
        ORDER BY
            current.total DESC,
            ih.last_import ASC NULLS FIRST;
    """

    with osmdb.cursor(row_factory=dict_row) as cursor:
        brands = cursor.execute(query).fetchall()

    return brands


def current_wave(cursor: Cursor, brand_wikidata: str) -> Wave:
    """The wave the brand is on: the first that still has something to give.

    Falls back to the last wave when nothing is left at all — /validate then
    finds an empty batch and closes the brand, as it always has.
    """
    row = cursor.execute(
        f"""SELECT MIN(wave) AS wave
            FROM ({UNBLOCKED_WAVES_SQL}) per_wave
            WHERE brand_wikidata = %s""",
        (brand_wikidata,),
    ).fetchone()
    number = (row or {}).get("wave")
    return WAVES_BY_NUMBER.get(number, WAVES[-1])


def apply_tag(tags: dict, key: str, value: Any) -> None:
    if value is None:
        return
    if key not in tags:
        # Check for not:key with the same value - if it exists, don't apply the tag
        not_key = f"not:{key}"
        if not_key in tags and tags[not_key] == value:
            return
        tags[key] = value


# The rules normalize_opening_hours reads, mirrored: weekdays (or none) then
# times, off/closed or 24/7, an explicit `open` allowed. The other rules of
# a value are what ATP never scrapes. `PH` may sit in the day list: the
# holiday is kept as a rule of its own, the weekdays are what got replaced.
_DAY = r"(?:Mo|Tu|We|Th|Fr|Sa|Su)"
_SPEC = r"(?:\d{1,2}:\d{2}-\d{1,2}:\d{2}(?:,\d{1,2}:\d{2}-\d{1,2}:\d{2})*|off|closed|24/7)"
_WEEKDAY_RULE = re.compile(rf"^({_DAY}(?:-{_DAY})?(?:,{_DAY}(?:-{_DAY})?)*)? ?{_SPEC}(?: open)?$")
_WEEKDAY_AND_PH_RULE = re.compile(
    rf"^((?:{_DAY}(?:-{_DAY})?|PH)(?:,(?:{_DAY}(?:-{_DAY})?|PH))*) ?({_SPEC})(?: open)?$"
)
_PH_RULE = re.compile(rf"^PH ?{_SPEC}(?: open)?$")
# A comma between two rules: the second is additional to the first.
_COMMA_BETWEEN_RULES = re.compile(rf"(\d|off|closed|open)\s*,\s*(?=(?:{_DAY}|PH)\b)")
# What makes an unreadable rule speak of the week — the SQL function then
# returns NULL and the value is never proposed. Checked again here: a rule
# like that written back next to ATP's week would override it.
_SPEAKS_OF_WEEK = re.compile(rf"\b{_DAY}\b|\d{{1,2}}:\d{{2}}")


def merge_opening_hours(old: str, weekdays: str) -> str:
    """*old* with its weekday rules replaced by *weekdays*, the rest kept.

    ATP knows the week and nothing else: `PH off`, a `"sur rendez-vous"`
    comment, the `||` fallback were written by a contributor and stay. Where
    they stay matters — the rightmost rule wins, so `PH off; Mo-Su 09:00-18:00`
    opens on a holiday Monday and `Mo-Su 09:00-18:00; PH off` does not: the
    week takes the place of the first weekday rule, the other rules keep
    their order around it. Mirrors normalize_opening_hours, which left those
    very rules out of the comparison.
    """
    head, *fallback = old.split("||")
    rules = []
    for rule in _COMMA_BETWEEN_RULES.sub(r"\1;", head).split(";"):
        rule = rule.strip()
        if not rule:
            continue
        tidy = re.sub(r"\s*([,-])\s*", r"\1", re.sub(r"\s+", " ", rule))
        if _WEEKDAY_RULE.match(tidy):
            if weekdays not in rules:
                rules.append(weekdays)
        elif (m := _WEEKDAY_AND_PH_RULE.match(tidy)) and set(m[1].split(",")) > {"PH"}:
            if weekdays not in rules:
                rules.append(weekdays)
            rules.append(f"PH {m[2]}")
        elif _SPEAKS_OF_WEEK.search(tidy) and not _PH_RULE.match(tidy):
            raise ValueError(f"opening_hours rule not read as a week, would override it: {rule!r}")
        else:
            rules.append(rule)
    if weekdays not in rules:
        rules.insert(0, weekdays)
    return " || ".join(["; ".join(rules), *(f.strip() for f in fallback)])


def apply_on_node(atp_osm_match: dict, wave: int = 1) -> dict:
    new_tags = dict(atp_osm_match["tags"])

    if wave == 2:
        # The tags to replace were decided in SQL, next to the count that
        # announced them (see modifiable_tags in MATCHED_POI_SQL). Nothing is
        # added here: filling a hole is wave 1's business, and a brand is only
        # ever on one wave.
        #
        # Every writing present is rewritten — `phone` and `contact:phone`
        # alike, never created: an object tagged only `contact:phone` keeps
        # its spelling, one tagged both must not come out holding two
        # contradictory numbers.
        for key, value in (atp_osm_match.get("modifiable_tags") or {}).items():
            for written in (key, f"contact:{key}"):
                if written not in new_tags:
                    continue
                if key == "phone":
                    new_tags[written] = format_phone(value)
                elif key == "opening_hours":
                    new_tags[written] = merge_opening_hours(new_tags[written], value)
                else:
                    new_tags[written] = value
        return _change(atp_osm_match, new_tags)

    apply_tag(new_tags, "opening_hours", atp_osm_match["atp_opening_hours"])

    # Do not duplicate (contact:email and email) or (contact:phone and phone) or (contact:website and website) in tags
    if "contact:email" not in new_tags:
        apply_tag(new_tags, "email", atp_osm_match["atp_email"])
    if "contact:phone" not in new_tags:
        apply_tag(new_tags, "phone", format_phone(atp_osm_match["atp_phone"]))
    if "contact:website" not in new_tags:
        apply_tag(new_tags, "website", atp_osm_match["atp_website"])

    # NSI tags for this object, already narrowed down to a single brand entry
    # by mv_places (the object's own primary tag is the discriminator). Never
    # overwrites: apply_tag only fills what is missing, which is what keeps
    # NSI from reclassifying or renaming anything.
    for key, value in (atp_osm_match.get("nsi_tags") or {}).items():
        apply_tag(new_tags, key, value)

    return _change(atp_osm_match, new_tags)


def _change(atp_osm_match: dict, new_tags: dict) -> dict | None:
    """The proposal a wave produced, ready for the upload and the review."""
    # If new_tags and original ones are the same returns None to skip the update
    if new_tags == atp_osm_match["tags"]:
        return None

    # osm2pgsql's define_area_table stores relation IDs as negative values to
    # distinguish them from way IDs in the shared area_id column. Negate to
    # recover the real OSM ID before passing it to the API or the UI.
    osm_id = atp_osm_match["osm_id"]
    if osm_id < 0:
        osm_id = -osm_id

    return {
        # Values for bulk upload
        "id": osm_id,
        "node_type": atp_osm_match["node_type"],
        "version": atp_osm_match["version"],
        "tag": new_tags,
        "members": atp_osm_match.get("members"),
        "lon": atp_osm_match["lon"],
        "lat": atp_osm_match["lat"],
        # Values only for atp2osm render
        "atp_brand": atp_osm_match["brand"],
        "atp_id": atp_osm_match["id"],
        "spider_id": atp_osm_match.get("spider_id"),
        "source_uri": atp_osm_match["source_uri"],
        "source_type": atp_osm_match["source_type"],
        "postcode": atp_osm_match["postcode"],
        "old_tag": atp_osm_match["tags"],
        # When OSM last saw a change on this object — the first filter of the
        # wave-2 protection, and the only date that costs no API request.
        "osm_timestamp": atp_osm_match["osm_timestamp"].isoformat()
        if atp_osm_match.get("osm_timestamp")
        else None,
        # 'nsi' when the QID was recovered from a label rather than read on the
        # object: the reviewer is then validating an inference, and must see it.
        "brand_wikidata_source": atp_osm_match.get("brand_wikidata_source"),
        "subdivision_code": atp_osm_match["subdivision_code"],
        # Carried next to the code: the name comes from the OSM boundary at
        # attachment time, and a history row keeps the one it was written with.
        "subdivision_name": atp_osm_match["subdivision_name"],
    }


def add_result(nodes_by_brand, brand_wikidata, res):
    if brand_wikidata in nodes_by_brand:
        nodes_by_brand[brand_wikidata].append(res)
    else:
        nodes_by_brand[brand_wikidata] = [res]


def get_changes(cursor: Cursor, wave: int = 1):
    changes = []
    # seen = set()

    for atp_osm_match in cursor:
        # key = (atp_osm_match["osm_id"], atp_osm_match["node_type"])
        # if key in seen:
        #     continue
        # seen.add(key)

        res = apply_on_node(atp_osm_match, wave)
        if res is None:
            continue
        changes.append(res)

    return changes


def pack_subdivisions(counts: dict[str, int], max_size: int) -> list[list[str]]:
    """Group subdivisions into batches of at most *max_size* POIs.

    Greedy first-fit-decreasing: start a batch with the biggest subdivision left,
    then keep adding the biggest one that still fits, close the batch when none
    does. A subdivision bigger than *max_size* gets a batch of its own; the
    caller truncates it to *max_size* POIs and the remainder waits for the
    cooldown to expire.

    Returns batches as sorted lists of subdivision codes, biggest batch first.
    """
    # clamping makes an oversized subdivision fill a batch on its own
    remaining = sorted(
        ((sub, min(n, max_size)) for sub, n in counts.items()),
        key=lambda kv: (-kv[1], kv[0]),
    )
    batches = []

    while remaining:
        batch = [remaining.pop(0)]
        room = max_size - batch[0][1]
        # remaining stays sorted by size desc, so the first that fits is the biggest
        while (
            i := next((i for i, (_, n) in enumerate(remaining) if n <= room), None)
        ) is not None:
            sub, n = remaining.pop(i)
            batch.append((sub, n))
            room -= n
        batches.append(sorted(sub for sub, _ in batch))

    return batches


# Biggest batch, in POIs, of the wave that adds tags. A batch = a set of whole
# subdivisions, one changeset per subdivision. Each wave has its own size, in
# WAVES; this is the default the callers that predate them still take.
BATCH_MAX_SIZE = WAVES_BY_NUMBER[1].batch_size

# Hard ceiling: past that, a batch is refused rather than uploaded. Composition
# targets BATCH_MAX_SIZE, so the gap between the two is pure slack — it lets the
# beta cap be lowered without the safety nets firing on a legitimate batch.
MAX_UPLOAD_SIZE = 200

# Sample size of the wave that adds tags — the default the callers that
# predate the waves still take.
BATCH_SAMPLE_SIZE = WAVES_BY_NUMBER[1].sample_size


def changed_tags(change: dict) -> set[str]:
    """Keys whose value differs between the existing POI and the proposal."""
    tag = change.get("tag", {})
    old_tag = change.get("old_tag", {})
    return {k for k in tag.keys() | old_tag.keys() if tag.get(k) != old_tag.get(k)}


def sample_for_review(changes: list[dict], min_size: int = BATCH_SAMPLE_SIZE) -> list[dict]:
    """Sample reviewed before integration: at least one POI per changed tag.

    Its size therefore follows the number of tags involved, topped up at
    random up to *min_size*.
    """
    by_tag: dict[str, list[int]] = {}
    for i, change in enumerate(changes):
        for tag in changed_tags(change):
            by_tag.setdefault(tag, []).append(i)

    picked: set[int] = set()
    # ponytail: naive greedy, not a minimal cover — a few POIs too many at
    # worst, and the tag count stays single-digit.
    for candidates in by_tag.values():
        if not picked.intersection(candidates):
            picked.add(random.choice(candidates))

    rest = [i for i in range(len(changes)) if i not in picked]
    picked.update(random.sample(rest, max(0, min(min_size - len(picked), len(rest)))))
    return [changes[i] for i in sorted(picked)]


def subdivision_names(changes: list[dict]) -> dict[str, str]:
    """Code -> name, read off the changes themselves.

    The name travels with the POI instead of being looked up in a table: it is
    what the boundary was called when the batch was built, which is what the
    changeset comment and the history row have to say — even after a
    redistricting renames or splits the subdivision.
    """
    return {
        c["subdivision_code"]: c.get("subdivision_name") or c["subdivision_code"]
        for c in changes
        if c.get("subdivision_code") is not None
    }


def count_by_subdivision(changes: list[dict]) -> dict[str, int]:
    """Match count per subdivision."""
    counts = {}
    for change in changes:
        sub = change["subdivision_code"]
        counts[sub] = counts.get(sub, 0) + 1
    return counts


def get_blocked_subdivisions(
    cursor: Cursor, brand_wikidata: str, wave: int = 1
) -> set[str]:
    """Subdivisions of the brand still under cooldown, on that wave.

    The status that counts is the changeset's, not the import's: a changeset
    either went through or did not, there is no partial status at that level.
    """
    rows = cursor.execute(
        f"""SELECT DISTINCT subdivision_code AS sub
            FROM ({BLOCKED_DEPARTEMENTS_SQL}) b
            WHERE brand_wikidata = %s AND wave = %s""",
        (brand_wikidata, wave),
    ).fetchall()
    return {row["sub"] for row in rows}  # dict_row cursor, as everywhere here


def compose_batch(
    counts: dict[str, int], blocked: set[str], max_size: int = BATCH_MAX_SIZE
) -> list[str]:
    """Subdivisions of the next batch to integrate.

    Never persisted: recomputed on every visit from the current state. Returns an
    empty list when every subdivision is blocked.
    """
    available = {sub: n for sub, n in counts.items() if sub not in blocked}
    batches = pack_subdivisions(available, max_size)
    return batches[0] if batches else []


def select_batch(
    changes: list[dict], blocked: set[str], max_size: int = BATCH_MAX_SIZE
) -> list[dict]:
    """Narrow matches down to the next batch.

    A batch is made of whole subdivisions: one that does not fit in the room left
    moves to the next batch, it is never cut. A batch below *max_size* is
    therefore normal — it happens as soon as no remaining subdivision fills the
    gap.

    The only possible truncation is a subdivision bigger than *max_size* on its
    own: it then forms a batch by itself (pack_subdivisions leaves it no room),
    and its extra POIs wait for the cooldown to expire.

    """
    batch = set(compose_batch(count_by_subdivision(changes), blocked, max_size))
    changes = [c for c in changes if c["subdivision_code"] in batch]
    # A no-op on a multi-subdivision batch, which fits in max_size by
    # construction. Truncating before the sample is drawn keeps the review on
    # POIs that will actually be integrated.
    return changes[:max_size]


def batch_scope(changes: list[dict]) -> list[dict]:
    """The subdivisions a batch covers, biggest first — what /validate announces."""
    names = subdivision_names(changes)
    return [
        {"number": sub, "name": names[sub], "count": count}
        for sub, count in sorted(
            count_by_subdivision(changes).items(), key=lambda kv: (-kv[1], kv[0])
        )
    ]


def get_stats(changes: list) -> dict:
    tag_updates = {}
    total_tag_updates = 0
    sub_changes = {}

    for change in changes:
        # Count tag updates
        for t in changed_tags(change):
            tag_updates[t] = tag_updates.get(t, 0) + 1
            total_tag_updates += 1

        # Count changes by department
        sub = change.get("subdivision_code")
        if sub is not None:
            sub_changes[sub] = sub_changes.get(sub, 0) + 1

    names = subdivision_names(changes)
    by_subdivision = {
        sub: {"name": names[sub], "count": count}
        for sub, count in sorted(sub_changes.items())
    }

    return {
        "by_tag": tag_updates,
        "size": len(changes),
        "total_tag_updates": total_tag_updates,
        "by_subdivision": by_subdivision,
    }
