-- Waves: a brand is integrated one typology at a time (spec 04). The column
-- says which one an integration belonged to, and confines its cooldown to that
-- wave — a subdivision added to in wave 1 must not block its modification in
-- wave 2. Everything before this migration was an addition, hence wave 1.
--
-- tag_counts freezes the per-tag detail of a changeset. Until now it only
-- existed on screen, recomputed from the matches; the next refresh drops the
-- match and the count becomes unreconstructible. Benign for an addition, it is
-- the only measure that matters for a modification: which tag was replaced,
-- and how many times.

ALTER TABLE import_history ADD COLUMN IF NOT EXISTS wave SMALLINT NOT NULL DEFAULT 1;
ALTER TABLE import_subdivisions ADD COLUMN IF NOT EXISTS tag_counts JSONB;
