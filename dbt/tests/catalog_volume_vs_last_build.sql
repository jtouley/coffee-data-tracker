-- A pipeline-wide break must not be published: listed coffees, in-stock coffees and roasters may move at most
-- volume_tolerance from the baseline, the median of the last 4 passing builds up to the release customers get now
-- (var baseline_build; after a rollback that is not the newest passing build, and the builds after it don't
-- count), or else up to the newest passing build. A median, so one odd build doesn't move the baseline. The first
-- build has nothing to compare with.
with b as (select *, row_number() over (order by built_at, build_id) as seq from {{ ref('stg_builds') }}),
served as (
    {% if var('baseline_build') %}
    select seq from b where build_id = '{{ var("baseline_build") }}'
    {% else %}
    select max(seq) as seq from b where passed
    {% endif %}
),
baseline_builds as (
    select b.build_id from b join served on b.seq <= served.seq where b.passed order by b.seq desc limit 4
),
baseline as (
    select unique_id as metric, median(value) as value
    from {{ lake('builds') }} join baseline_builds using (build_id)
    where kind = 'metric'
    group by unique_id
)
select p.metric, baseline.value as baseline_value, p.value as current_value
from {{ ref('mart_build_profile') }} p
join baseline using (metric)
where p.metric in ('listed_coffees', 'in_stock_coffees', 'roasters_with_coffee')
  and baseline.value > 0
  and abs(p.value - baseline.value) / baseline.value > {{ var('volume_tolerance') }}
