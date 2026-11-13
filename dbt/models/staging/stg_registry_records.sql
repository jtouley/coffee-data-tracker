-- The lake's registry records (raw registry: roasters added by discovery or by hand), one row per distinct record,
-- each with whether its source may feed the public tables (is_open_source: Overture, curated, manual; never
-- OpenStreetMap, which is ODbL). stg_registry picks each roaster's values from these; the test
-- public_values_have_open_provenance checks every published registry value against them.
-- A record repeated exactly is one row: every pick stg_registry makes from these is unchanged by a repeat.
-- A row without a roaster_id (the lake's zero-row schema file reads as none) is left out.
select distinct
    roaster_id, source, added_at, name, city, state, locations, opted_out,
    {{ is_open_source('source') }} as is_open_source
from {{ lake('registry') }}
where roaster_id is not null
