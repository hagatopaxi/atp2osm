-- The weekday part of an opening_hours value, written the way OSM writes it.
--
-- ATP produces a strict subset of the syntax, and a different dialect from
-- the contributors': `Su closed` for an omitted day, `Mo-Su 00:00-24:00` for
-- `24/7`, a rule per day where a mapper writes `Tu-We,Fr`, a range crossing
-- midnight split over two days. The same hours in two spellings must not
-- make a changeset, so both sides are reduced to one canonical writing
-- before being compared.
--
-- Only the rules ATP can express take part: a comma list of weekdays (or
-- none, meaning every day) followed by time ranges, off/closed or 24/7.
-- A rule that says nothing about weekdays or times — `PH off`, a
-- `"comment"`, the `||` fallback — is left out here and kept verbatim at
-- write time (see merge_opening_hours): ATP never scrapes it, so it is
-- neither compared nor overwritten.
--
-- Two frequent liberties are read too: a comma between rules where the
-- syntax wants a semicolon (`Mo-Fr 10:30-19:30, Sa 09:45-19:30`), and `PH`
-- in a day list (`Su,PH 09:00-12:00`) — the weekdays are compared, the
-- holiday is the contributor's and merge_opening_hours keeps it as a rule
-- of its own. A rule about `PH` alone says nothing about the week.
--
-- A rule that does speak of weekdays or times but in a way this function
-- cannot read — `Jan-Mar Mo-Fr 09:00-12:00`, `Mo-Fr 09:00-12:00 "sur rdv"`,
-- `Mo-Fr 09:00+`, a lowercase `mo-fr` — makes the whole value unreadable:
-- NULL, so that it is never compared nor written over. Written back after
-- ATP's week, such a rule would override it (OSM reads the last rule); and
-- seasonal hours flattened to one week would be a loss, not a fix.
--
-- The rules are read with OSM semantics: a later rule overrides an earlier
-- one on the days it names. Returns NULL when no weekday rule is left.

CREATE OR REPLACE FUNCTION normalize_opening_hours(oh TEXT) RETURNS TEXT
LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE AS $$
DECLARE
  day_names CONSTANT TEXT[] := ARRAY['Mo','Tu','We','Th','Fr','Sa','Su'];
  day_re    CONSTANT TEXT := '(?:Mo|Tu|We|Th|Fr|Sa|Su)';
  time_re   CONSTANT TEXT := '\d{1,2}:\d{2}-\d{1,2}:\d{2}';
  spec_re   TEXT;
  rule_re   TEXT;
  -- Per day, the open ranges as "start-end" minutes, comma-joined ("" = closed).
  hours     TEXT[] := ARRAY['','','','','','',''];
  rule      TEXT;
  m         TEXT[];
  spec      TEXT;
  d         INT;
  d_from    INT;
  d_to      INT;
  nd        INT;
  s         INT;
  e         INT;
  tr        TEXT;
  ranges    TEXT[];
  nranges   TEXT[];
  found     BOOLEAN;
  out_rules TEXT[] := '{}';
  seen      TEXT[] := '{}';
  group_days INT[];
  run_start INT;
  day_spec  TEXT[];
  h         TEXT;
BEGIN
  IF btrim(oh) = '24/7' THEN
    RETURN '24/7';
  END IF;

  spec_re := time_re || '(?:,' || time_re || ')*|off|closed|24/7';
  rule_re := '^(' || day_re || '(?:-' || day_re || ')?(?:,' || day_re || '(?:-' || day_re || ')?)*)?'
          || ' ?(' || spec_re || ')$';

  FOREACH rule IN ARRAY regexp_split_to_array(
      regexp_replace(split_part(oh, '||', 1), '(\d|off|closed)\s*,\s*(?=(?:' || day_re || '|PH)\M)', '\1;', 'g'),
      ';') LOOP
    rule := regexp_replace(regexp_replace(btrim(rule), '\s*([,-])\s*', '\1', 'g'), '\s+', ' ', 'g');
    rule := regexp_replace(regexp_replace(rule, '^PH,', ''), ',PH(?=[ ,])', '', 'g');
    m := regexp_match(rule, rule_re);
    IF m IS NULL THEN
      IF rule !~ ('^PH ?(?:' || spec_re || ')$')
         AND rule ~ ('(?:\m' || day_re || '\M|\d{1,2}:\d{2})') THEN
        RETURN NULL;
      END IF;
      CONTINUE;
    END IF;

    -- The ranges in minutes, sorted, those that touch merged: ATP writes
    -- `00:00-12:30,12:30-24:00` for a full day.
    spec := '';
    IF m[2] = '24/7' THEN
      spec := '0-1440';
    ELSIF m[2] NOT IN ('off', 'closed') THEN
      ranges := '{}';
      FOREACH tr IN ARRAY string_to_array(m[2], ',') LOOP
        s := split_part(split_part(tr, '-', 1), ':', 1)::INT * 60 + split_part(split_part(tr, '-', 1), ':', 2)::INT;
        e := split_part(split_part(tr, '-', 2), ':', 1)::INT * 60 + split_part(split_part(tr, '-', 2), ':', 2)::INT;
        IF e <= s THEN e := e + 1440; END IF;
        ranges := ranges || (s || '-' || e);
      END LOOP;
      s := NULL;
      FOREACH tr IN ARRAY (SELECT array_agg(r ORDER BY split_part(r, '-', 1)::INT) FROM unnest(ranges) AS r)
                       || ARRAY['9999-9999'] LOOP
        IF s IS NOT NULL AND split_part(tr, '-', 1)::INT <> e THEN
          spec := spec || CASE WHEN spec = '' THEN '' ELSE ',' END || s || '-' || e;
          s := NULL;
        END IF;
        IF s IS NULL THEN s := split_part(tr, '-', 1)::INT; END IF;
        e := split_part(tr, '-', 2)::INT;
      END LOOP;
    END IF;

    IF m[1] IS NULL THEN
      hours := ARRAY[spec, spec, spec, spec, spec, spec, spec];
    ELSE
      FOREACH tr IN ARRAY string_to_array(m[1], ',') LOOP
        d_from := array_position(day_names, split_part(tr, '-', 1));
        d_to := COALESCE(array_position(day_names, NULLIF(split_part(tr, '-', 2), '')), d_from);
        d := d_from;
        LOOP
          hours[d] := spec;
          EXIT WHEN d = d_to;
          d := d % 7 + 1;
        END LOOP;
      END LOOP;
    END IF;
  END LOOP;

  -- A range ending at midnight followed, the next day, by one starting at
  -- midnight is one range crossing it: `Mo 22:00-24:00; Tu 00:00-02:00` is
  -- how ATP writes `Mo 22:00-02:00`. A full day (00:00-24:00) is left alone.
  FOR d IN 1..7 LOOP
    nd := d % 7 + 1;
    nranges := '{}';
    FOREACH tr IN ARRAY string_to_array(hours[d], ',') LOOP
      s := split_part(tr, '-', 1)::INT;
      e := split_part(tr, '-', 2)::INT;
      IF e = 1440 AND s > 0 THEN
        found := false;
        ranges := '{}';
        FOREACH h IN ARRAY string_to_array(hours[nd], ',') LOOP
          IF NOT found AND split_part(h, '-', 1)::INT = 0 AND split_part(h, '-', 2)::INT < 1440 THEN
            e := 1440 + split_part(h, '-', 2)::INT;
            found := true;
          ELSE
            ranges := ranges || h;
          END IF;
        END LOOP;
        IF found THEN hours[nd] := array_to_string(ranges, ','); END IF;
      END IF;
      nranges := nranges || (s || '-' || e);
    END LOOP;
    hours[d] := array_to_string(nranges, ',');
  END LOOP;

  IF hours = ARRAY['0-1440','0-1440','0-1440','0-1440','0-1440','0-1440','0-1440'] THEN
    RETURN '24/7';
  END IF;

  -- One rule per distinct set of hours, its days as `Mo-We,Fr`, in the order
  -- of their first day. Closed days are simply not written.
  FOR d IN 1..7 LOOP
    CONTINUE WHEN hours[d] = '' OR hours[d] = ANY(seen);
    seen := seen || hours[d];
    group_days := '{}';
    FOR nd IN d..7 LOOP
      IF hours[nd] = hours[d] THEN group_days := group_days || nd; END IF;
    END LOOP;

    day_spec := '{}';
    run_start := group_days[1];
    FOR nd IN 1..array_length(group_days, 1) LOOP
      IF nd = array_length(group_days, 1) OR group_days[nd + 1] <> group_days[nd] + 1 THEN
        day_spec := day_spec || CASE
          WHEN run_start = group_days[nd] THEN day_names[run_start]
          ELSE day_names[run_start] || '-' || day_names[group_days[nd]] END;
        run_start := group_days[nd + 1];
      END IF;
    END LOOP;

    ranges := '{}';
    FOREACH tr IN ARRAY (SELECT array_agg(r ORDER BY split_part(r, '-', 1)::INT)
                         FROM unnest(string_to_array(hours[d], ',')) AS r) LOOP
      s := split_part(tr, '-', 1)::INT;
      e := split_part(tr, '-', 2)::INT;
      ranges := ranges || (
        lpad((s / 60)::TEXT, 2, '0') || ':' || lpad((s % 60)::TEXT, 2, '0') || '-'
        || CASE WHEN e = 1440 THEN '24:00'
                ELSE lpad(((e % 1440) / 60)::TEXT, 2, '0') || ':' || lpad((e % 60)::TEXT, 2, '0') END);
    END LOOP;

    out_rules := out_rules || (array_to_string(day_spec, ',') || ' ' || array_to_string(ranges, ','));
  END LOOP;

  RETURN NULLIF(array_to_string(out_rules, '; '), '');
END;
$$;
