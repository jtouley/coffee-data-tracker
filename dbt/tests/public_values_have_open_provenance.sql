-- Field-level provenance (#90): every registry value a public table publishes (a roaster's name, city and state, in
-- public_roaster and again in public_coffee) is one an open source gave for that roaster: a lake registry record from
-- Overture, the curated seed or a manual add (stg_registry_records.is_open_source), or the seed list when its
-- sources are open. A value only OpenStreetMap (ODbL) gave, or any other source, fails the build, one row per
-- table, roaster and field. public_roaster's own filter keeps out roasters known only from OpenStreetMap; this holds
-- the line for each value of the roasters it lets in. Checked against the records themselves, not stg_registry's
-- pick, so a change to the pick can't pass by agreeing with itself.
with open_values as (
    select roaster_id, 'name' as field, name as value from {{ ref('stg_registry_records') }} where is_open_source
    union all
    select roaster_id, 'city', city from {{ ref('stg_registry_records') }} where is_open_source
    union all
    select roaster_id, 'state', state from {{ ref('stg_registry_records') }} where is_open_source
    union all
    select roaster_id, 'name', name from {{ ref('stg_roaster_seed') }} where {{ is_open_source('sources') }}
    union all
    select roaster_id, 'city', city from {{ ref('stg_roaster_seed') }} where {{ is_open_source('sources') }}
    union all
    select roaster_id, 'state', state from {{ ref('stg_roaster_seed') }} where {{ is_open_source('sources') }}
),
published as (
    select 'public_roaster' as public_table, roaster_id, 'name' as field, roaster_name as value
    from {{ ref('public_roaster') }}
    union all
    select 'public_roaster', roaster_id, 'city', city from {{ ref('public_roaster') }}
    union all
    select 'public_roaster', roaster_id, 'state', state from {{ ref('public_roaster') }}
    union all
    select distinct 'public_coffee', roaster_id, 'name', roaster_name from {{ ref('public_coffee') }}
    union all
    select distinct 'public_coffee', roaster_id, 'state', roaster_state from {{ ref('public_coffee') }}
)
select p.public_table, p.roaster_id, p.field
from published p
where p.value is not null
  and not exists (
      select 1 from open_values o
      where o.roaster_id = p.roaster_id and o.field = p.field and o.value = p.value
  )
