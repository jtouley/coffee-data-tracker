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
    select c.roaster_id, c.source, c.added_at, c.name, c.city, c.state, c.locations, false as opted_out
    from {{ lake('candidates') }} c
    where c.roaster_id in ({{ sql_strings(flagged) }})
      -- only the same record (roaster and source) the registry already holds is left out, so it isn't repeated; a
      -- record of the same roaster from another source (OpenStreetMap) doesn't hide the place's open one
      and not exists (select 1 from {{ lake('registry') }} r
                      where r.roaster_id = c.roaster_id and r.source is not distinct from c.source)
    {%- endif %}
)
where roaster_id is not null
