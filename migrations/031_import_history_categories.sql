-- What a review decided, type by type: the primary tags its batch carried
-- (`{"shop=bakery"}`) and the ones the reviewer took out (`{"amenity=fuel"}`),
-- as chosen on /validate.
--
-- The two lists together say everything: a type is in one or the other, and
-- one that is in neither was not in the batch to be decided on — the next
-- review of the brand ticks it, as it does any type it has never been told
-- about, and unticks what the last one left out.
--
-- NULL on both means no choice was recorded at all: the integrations older
-- than the filter.
ALTER TABLE import_history ADD COLUMN IF NOT EXISTS included_categories TEXT[];
ALTER TABLE import_history ADD COLUMN IF NOT EXISTS excluded_categories TEXT[];
