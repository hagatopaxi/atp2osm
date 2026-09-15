-- node/5037224542, an ATM tagged brand="Caisse d'Épargne" and
-- not:brand:wikidata=Q1547738: a contributor had already ruled the brand out,
-- and the name lookup inferred that very QID back, matching the object to the
-- brand's ATP data. A denied QID is never recovered from a name.
CREATE OR REPLACE FUNCTION nsi_match(osm_tags jsonb) RETURNS jsonb AS $$
DECLARE
    prim text[] := osm_primary_tag(osm_tags);
    qid  text   := osm_tags->>'brand:wikidata';
    hit  jsonb;
    n    integer;
BEGIN
    IF qid IS NOT NULL THEN
        SELECT count(*) INTO n FROM (
            SELECT DISTINCT primary_key, primary_value
            FROM nsi_brands WHERE brand_wikidata = qid
        ) categories;

        IF n = 1 THEN
            SELECT CASE
                     WHEN prim IS NULL OR prim = ARRAY[primary_key, primary_value]
                     THEN tags
                     ELSE tags - primary_key
                   END
              INTO hit FROM nsi_brands
             WHERE brand_wikidata = qid LIMIT 1;
            RETURN hit;
        END IF;

        IF n = 0 OR prim IS NULL THEN
            RETURN NULL;
        END IF;

        -- NULL when no category matches: NSI never reclassifies an object.
        SELECT tags INTO hit FROM nsi_brands
         WHERE brand_wikidata = qid
           AND primary_key = prim[1] AND primary_value = prim[2]
         LIMIT 1;
        RETURN hit;
    END IF;

    IF prim IS NULL THEN
        RETURN NULL;
    END IF;

    SELECT count(DISTINCT brand_wikidata) INTO n FROM nsi_brands
     WHERE primary_key = prim[1] AND primary_value = prim[2]
       AND brand_wikidata IS DISTINCT FROM osm_tags->>'not:brand:wikidata'
       AND (LOWER(brand) = LOWER(osm_tags->>'brand')
            OR LOWER(name) = LOWER(osm_tags->>'name'));

    IF n <> 1 THEN
        RETURN NULL;
    END IF;

    SELECT tags INTO hit FROM nsi_brands
     WHERE primary_key = prim[1] AND primary_value = prim[2]
       AND brand_wikidata IS DISTINCT FROM osm_tags->>'not:brand:wikidata'
       AND (LOWER(brand) = LOWER(osm_tags->>'brand')
            OR LOWER(name) = LOWER(osm_tags->>'name'))
     LIMIT 1;
    RETURN hit;
END;
$$ LANGUAGE plpgsql STABLE SET search_path = public, pg_temp;
