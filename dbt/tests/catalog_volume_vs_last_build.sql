-- A pipeline-wide break must not be published: listed coffees, in-stock coffees and roasters may move at most
-- volume_tolerance from the release customers get now (var baseline_build; after a rollback that is not the newest
-- passing build), or else the last build whose tests all passed. The first build has nothing to compare with.
-- fresh_listed_coffees (listed coffees this run fetched itself) is checked too: carry-forward keeps listed_coffees
-- steady through a fetch outage, this one moves. A baseline recorded before it existed has nothing to compare.
with last_build as (
    {% if var('baseline_build') %}
    select '{{ var("baseline_build") }}' as build_id
    {% else %}
    select build_id from {{ ref('stg_builds') }} where passed order by built_at desc, build_id desc limit 1
    {% endif %}
),
previous as (
    select unique_id as metric, value
    from {{ lake('builds') }} join last_build using (build_id)
    where kind = 'metric'
)
select p.metric, previous.value as previous_value, p.value as current_value
from {{ ref('mart_build_profile') }} p
join previous using (metric)
where p.metric in ('listed_coffees', 'fresh_listed_coffees', 'in_stock_coffees', 'roasters_with_coffee')
  and previous.value > 0
  and abs(p.value - previous.value) / previous.value > {{ var('volume_tolerance') }}
