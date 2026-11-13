-- The ref to stg_builds below sits inside a var check (it is read only without var baseline_build); this line
-- keeps the dependency whatever the vars (docs/dbt/CONVENTION.md, section 5).
-- depends_on: {{ ref('stg_builds') }}
-- A pipeline-wide break must not be published: listed coffees, in-stock coffees and roasters may move at most
-- volume_tolerance from the release customers get now (var baseline_build; after a rollback that is not the newest
-- passing build), or else the last build whose tests all passed. The first build has nothing to compare with.
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
where p.metric in ('listed_coffees', 'in_stock_coffees', 'roasters_with_coffee')
  and previous.value > 0
  and abs(p.value - previous.value) / previous.value > {{ var('volume_tolerance') }}
