-- The primary OSM tag of an ATP POI (`{amenity,kindergarten}`), same shape as
-- osm_primary_tag() returns for an OSM object: MATCHED_POI_SQL compares the two
-- before trusting a name alone.
--
-- atp_places is rebuilt by the pipeline, so the column is NULL until the next
-- ATP import. NULL is the tolerant value the join already reads as "ATP says
-- nothing about the category", which is also the 4% of POIs that carry none.
ALTER TABLE IF EXISTS atp_places ADD COLUMN IF NOT EXISTS category TEXT[];
