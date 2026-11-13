-- The lake's registry records (raw registry: roasters added by discovery or by hand), one row per raw record, each
-- with whether its source may feed the public tables (is_open_source: the record's whole source is Overture,
-- curated or manual; never OpenStreetMap, which is ODbL, a name that only starts like an open one, or several
-- sources at once). stg_registry picks each roaster's values from these; the test public_values_have_open_provenance
-- checks every published registry value against them.
-- A row without a roaster_id (the lake's zero-row schema file reads as none) is left out.
-- The places of the source list (`candidates`) the build's snapshot flags as roasters (var source_roasters; the
-- admin's Sources page) count as records too, as if discovery had added them (registry.state_sql does the same).
select
    roaster_id, source, added_at, name, city, state, locations, opted_out,
    {{ is_open_record_source('source') }} as is_open_source
from (
    select roaster_id, source, added_at, name, city, state, locations, opted_out from {{ lake('registry') }}
    {%- set flagged = var('source_roasters', []) %}
    {%- if flagged %}
    union all
    select roaster_id, source, added_at, name, city, state, locations, false as opted_out
    from {{ lake('candidates') }}
    where roaster_id in ({{ sql_strings(flagged) }})
      -- one the registry already lists is a record there: its place adds nothing (and would repeat the record)
      and roaster_id not in (select roaster_id from {{ lake('registry') }} where roaster_id is not null)
    {%- endif %}
)
where roaster_id is not null
