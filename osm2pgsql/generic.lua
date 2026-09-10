local srid = 4326

-- The pipeline points this at a schema of its own, beside the live tables:
-- --create starts the tables from scratch, and the site reads the live ones
-- for the hours the import takes. Unset, everything lands in public.
local schema = os.getenv("ATP2OSM_IMPORT_SCHEMA")

local tables = {}

tables.points = osm2pgsql.define_node_table('points', {
    { column = 'tags',    type = 'jsonb' },
    { column = 'geom',    type = 'point', projection = srid, not_null = true },
    { column = 'version', type = 'int' },
    -- Epoch seconds: the flex output has no timestamp column type. mv_places
    -- turns it back into a timestamptz with to_timestamp().
    { column = 'osm_timestamp', type = 'int8' },
}, { schema = schema })

tables.polygons = osm2pgsql.define_area_table('polygons', {
    { column = 'osm_type', type = 'text',     not_null = true },
    { column = 'tags',     type = 'jsonb' },
    { column = 'members',  type = 'jsonb' },
    { column = 'geom',     type = 'geometry', projection = srid, not_null = true },
    { column = 'version',  type = 'int' },
    -- Epoch seconds: the flex output has no timestamp column type. mv_places
    -- turns it back into a timestamptz with to_timestamp().
    { column = 'osm_timestamp', type = 'int8' },
}, { schema = schema })

-- Administrative boundaries, the source of the subdivision a POI is attached to.
-- Imported down to ATP2OSM_ADMIN_LEVEL_MAX, which is deliberately deeper than
-- the level the attachment reads: lowering that one is then a SQL filter, not a
-- reimport of 20 GB of PBF.
local admin_level_max = tonumber(os.getenv("ATP2OSM_ADMIN_LEVEL_MAX")) or 8

tables.subdivisions = osm2pgsql.define_area_table('subdivisions', {
    -- The OSM relation id, the fallback identifier when ref is missing.
    -- define_area_table adds an area_id of its own, which is not it.
    { column = 'osm_id',      type = 'int8', not_null = true },
    -- The official code: ref:INSEE in France, plain ref in most countries,
    -- and the ISO code for a country — which is what identifies level 2.
    { column = 'ref',         type = 'text' },
    { column = 'name',        type = 'text', not_null = true },
    { column = 'admin_level', type = 'int',  not_null = true },
    { column = 'geom',        type = 'geometry', projection = srid, not_null = true },
}, { schema = schema })

-- Insert the object as a subdivision and report it, so the caller stops there:
-- a boundary is never a place, and the POI filters below would drop it anyway.
-- ponytail: relations only. Boundaries mapped as a closed way exist, but not at
-- levels 2-8 in the countries served so far; revisit if one turns up missing.
local function insert_subdivision(object)
    local tags = object.tags
    if tags['type'] ~= 'boundary' or tags['boundary'] ~= 'administrative' then
        return false
    end
    local level = tonumber(tags['admin_level'])
    if not level or level < 2 or level > admin_level_max then return false end
    if not tags['name'] then return false end

    tables.subdivisions:insert({
        osm_id = object.id,
        ref = tags['ref:INSEE'] or tags['ref']
            or tags['ISO3166-1:alpha2'] or tags['ISO3166-1'],
        name = tags['name'],
        admin_level = level,
        geom = object:as_multipolygon(),
    })
    return true
end

-- Based on tags wiki list, that removes every POI which are definitely not places
-- https://wiki.openstreetmap.org/wiki/Map_features
local function is_definitely_not_a_place(tags)
    if tags["advertising"] then return true end
    if tags["aerialway"] then return true end
    if tags["aeroway"] then return true end
    if tags["barrier"] then return true end
    if tags["bicycle_road"] then return true end
    if tags["boundary"] then return true end
    if tags["admin_level"] then return true end
    if tags["busway"] then return true end
    if tags["cycleway"] then return true end
    if tags["emergency"] then return true end
    if tags["geological"] then return true end
    if tags["footway"] then return true end
    if tags["highway"] then return true end
    if tags["lifeguard"] then return true end
    if tags["man_made"] then return true end
    if tags["military"] then return true end
    if tags["natural"] then return true end
    if tags["parking"] then return true end
    if tags["place"] then return true end
    if tags["power"] then return true end
    if tags["public_transport"] then return true end
    if tags["railway"] then return true end
    if tags["route"] then return true end
    if tags["sidewalk"] then return true end
    if tags["telecom"] then return true end
    if tags["traffic_sign"] then return true end
    if tags["water"] then return true end
    if tags["waterway"] then return true end

    if tags["bicycle_road"] then return true end

    if tags["building"] and not (tags["shop"] or tags["brand"] or tags["brand:wikidata"]) then return true end

    if tags["landuse"] == 'industrial' then return true end
    if tags["landuse"] == 'construction' then return true end
    if tags["landuse"] == 'aquaculture' then return true end
    if tags["landuse"] == 'farmyard' then return true end
    if tags["landuse"] == 'flowerbed' then return true end
    if tags["landuse"] == 'farmyard' then return true end
    if tags["landuse"] == 'depot' then return true end
    if tags["landuse"] == 'quarry' then return true end
    if tags["landuse"] == 'railway' then return true end

    if tags["railway"] and tags["railway"] ~= 'halt' then return true end
    if tags["railway"] and tags["railway"] ~= 'stop_position' then return true end
    if tags["railway"] and tags["railway"] ~= 'stop' then return true end
    if tags["railway"] and tags["railway"] ~= 'station' then return true end
    if tags["railway"] and tags["railway"] ~= 'platform' then return true end
    if tags["railway"] and tags["railway"] ~= 'subway_entrance' then return true end
    if tags["railway"] and tags["railway"] ~= 'tram_stop' then return true end

    if tags["amenity"] then
        local amenity = tags["amenity"]

        if amenity == 'shop' then return false end

        if amenity == 'bar' then return false end
        if amenity == 'biergarten' then return false end
        if amenity == 'cafe' then return false end
        if amenity == 'fast_food' then return false end
        if amenity == 'food_court' then return false end
        if amenity == 'ice_cream' then return false end
        if amenity == 'pub' then return false end
        if amenity == 'restaurant' then return false end
        if amenity == 'atm' then return false end
        if amenity == 'bank' then return false end
        if amenity == 'bureau_de_change' then return false end
        if amenity == 'money_transfer' then return false end
        if amenity == 'payment_centre' then return false end
        if amenity == 'bicycle_rental' then return false end
        if amenity == 'boat_rental' then return false end
        if amenity == 'car_rental' then return false end
        if amenity == 'fuel' then return false end
        if amenity == 'motorcycle_rental' then return false end

        -- All other amenities are rejected for ATP
        return true
    end

    return false
end

-- The ATP <-> OSM join requires an equality on one of these attributes, and NULL
-- never equals anything: an object carrying none of them can never be matched.
-- Skipping them here drops ~95% of the rows (20.3M -> 1.1M), which is time and
-- disk saved on every weekly import. mv_places filters on the same keys, so a
-- stale import cannot bring them back either.
local matchable_keys = {
    'name', 'brand', 'brand:wikidata',
    'email', 'contact:email',
    'phone', 'contact:phone',
    'website', 'contact:website',
}

local function has_no_matchable_tag(tags)
    for _, key in ipairs(matchable_keys) do
        if tags[key] then return false end
    end
    return true
end

-- Filter out disused POIs: objects with disused=* or disused:*=* tags
-- should not be matched with ATP data as they are no longer active.
local function has_disused_tag(tags)
    if tags['disused'] then return true end
    local prefix = 'disused:'
    local prefix_len = #prefix
    for key, _ in pairs(tags) do
        if #key >= prefix_len and key:sub(1, prefix_len) == prefix then
            return true
        end
    end
    return false
end

function osm2pgsql.process_way(object)
    local tags = object.tags
    if is_definitely_not_a_place(tags) then return end
    if has_disused_tag(tags) then return end
    if has_no_matchable_tag(tags) then return end

    if object.is_closed then
        tables.polygons:insert({
            osm_type = 'W',
            tags = object.tags,
            members = object.nodes,
            geom = object:as_polygon(),
            version = object.version,
            osm_timestamp = object.timestamp,
        })
    end
end

function osm2pgsql.process_node(object)
    local tags = object.tags
    if is_definitely_not_a_place(tags) then return end
    if has_disused_tag(tags) then return end
    if has_no_matchable_tag(tags) then return end

    tables.points:insert({
        tags = object.tags,
        geom = object:as_point(),
        version = object.version,
        osm_timestamp = object.timestamp,
    })
end

function osm2pgsql.process_relation(object)
    local tags = object.tags
    if insert_subdivision(object) then return end
    if is_definitely_not_a_place(tags) then return end
    if has_disused_tag(tags) then return end
    if has_no_matchable_tag(tags) then return end

    local relation_type = object.tags['type']

    if relation_type == 'multipolygon' then
        tables.polygons:insert({
            osm_type = 'R',
            tags = object.tags,
            members = object.members,
            geom = object:as_multipolygon(),
            version = object.version,
            osm_timestamp = object.timestamp,
        })
    end
end
