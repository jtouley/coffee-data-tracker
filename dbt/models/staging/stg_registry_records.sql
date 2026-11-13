-- The lake's registry records (raw registry: roasters added by discovery or by hand), one row per raw record, each
-- with whether its source may feed the public tables (is_open_source: the record's whole source is Overture,
-- curated or manual; never OpenStreetMap, which is ODbL, a name that only starts like an open one, or several
-- sources at once). stg_registry picks each roaster's values from these; the test public_values_have_open_provenance
-- checks every published registry value against them.
-- A row without a roaster_id (the lake's zero-row schema file reads as none) is left out.
select
    roaster_id, source, added_at, name, city, state, locations, opted_out,
    {{ is_open_record_source('source') }} as is_open_source
from {{ lake('registry') }}
where roaster_id is not null
