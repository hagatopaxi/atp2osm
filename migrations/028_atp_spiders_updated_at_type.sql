-- 027 added the column when it was missing; it can also be there at the wrong
-- type. DuckDB infers atp_spiders from spiders.json, and an ISO date with an
-- offset comes out VARCHAR (JSON when every date is null). The site compares
-- it to a TIMESTAMPTZ.
ALTER TABLE IF EXISTS atp_spiders ALTER COLUMN updated_at TYPE TIMESTAMPTZ
    USING NULLIF(updated_at::text, 'null')::timestamptz;
