-- atp_spiders is rebuilt by the pipeline, which added updated_at after the
-- table existed in production: NULL reads as "undated spider", which the
-- cooldown SQL already handles.
ALTER TABLE IF EXISTS atp_spiders ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
