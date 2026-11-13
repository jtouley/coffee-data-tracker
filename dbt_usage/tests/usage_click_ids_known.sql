-- The relationships check (warn): every click-out's roaster_id, and coffee_id when there is one, is in the served
-- release the build was given (stg_usage_catalog). Skipped when the build has no release. The collector checks an
-- id's shape only, never against a release, so this is the only check. Warn, not error: a click on a coffee the
-- page showed from an older release is real, and the numbers are directional (design section 4). A singular test
-- rather than dbt's relationships, because it must check only when a release is there.
{{ config(severity='warn') }}
with catalog as (
    select kind, id from {{ ref('stg_usage_catalog') }}
),

clicks as (
    select 'roaster' as kind, roaster_id as id from {{ ref('int_usage_events') }}
    where event = 'click_out' and roaster_id is not null
    union all
    select 'coffee', coffee_id from {{ ref('int_usage_events') }}
    where event = 'click_out' and coffee_id is not null
)

select c.kind, count(*) as clicks_unknown
from clicks as c
left join catalog as k on k.kind = c.kind and k.id = c.id
where k.id is null and (select count(*) from catalog) > 0
group by c.kind
