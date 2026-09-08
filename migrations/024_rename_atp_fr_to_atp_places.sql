-- The table holds the ATP POIs of the country this instance serves, whichever
-- it is; `atp_fr` named the France of the day the pipeline only knew France.
-- The pipeline recreates the table from the parquet on its next run, but the
-- rename keeps the site serving until then.
ALTER TABLE IF EXISTS atp_fr RENAME TO atp_places;
