-- A pipeline-wide break must not be published: listed coffees, in-stock coffees and roasters may move at most
-- volume_tolerance from the last build whose tests all passed. The first build has nothing to compare with.
with b as (select * from {{ lake('builds') }} where build_id is not null),
passing as (
    select build_id, max(built_at) as built_at
    from b group by build_id
    having not bool_or(coalesce(status, '') in ('error', 'fail'))
),
last_build as (select build_id from passing order by built_at desc limit 1),
previous as (
    select unique_id as metric, value from b join last_build using (build_id) where kind = 'metric'
)
select p.metric, previous.value as previous_value, p.value as current_value
from {{ ref('mart_build_profile') }} p
join previous using (metric)
where p.metric in ('listed_coffees', 'in_stock_coffees', 'roasters_with_coffee')
  and previous.value > 0
  and abs(p.value - previous.value) / previous.value > {{ var('volume_tolerance') }}
