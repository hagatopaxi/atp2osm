-- atp_spiders is rebuilt by the pipeline, which added log_url after the table
-- existed in production: NULL reads as "no log known", which /spiders shows
-- as a badge without a link.
ALTER TABLE IF EXISTS atp_spiders ADD COLUMN IF NOT EXISTS log_url TEXT;
