-- Current roaster state, derived from probe and fetch history (mirrors registry.STATE_SQL in Python, which
-- picks what to fetch; tests/test_dbt.py::test_python_and_dbt_agree_on_roaster_status checks the two agree).
with probes as (select * from {{ ref('stg_probes') }}),
last_probe as (select * from probes qualify row_number() over (partition by roaster_id order by probed_at desc) = 1),
last_good_probe as (
    select * from probes where status = 'ok'
    qualify row_number() over (partition by roaster_id order by probed_at desc) = 1
),
f as (select *, row_number() over (partition by roaster_id order by fetched_at desc) as rn from {{ ref('stg_fetches') }}),
-- Failure streaks count runs, not attempts: a run is ok if any attempt in it succeeded.
runs as (
    select roaster_id, run_date, bool_or(status = 'ok') as ok, max(fetched_at) as last_at,
           row_number() over (partition by roaster_id order by run_date desc) as rn
    from f group by roaster_id, run_date
),
recent as (
    select roaster_id, count(*) filter (where rn <= {{ var('max_failures') }}) as attempts,
           count(*) filter (where rn <= {{ var('max_failures') }} and not ok) as errors, max(last_at) as last_fetch
    from runs group by roaster_id
),
last_ok as (select * from f where status = 'ok' qualify row_number() over (partition by roaster_id order by fetched_at desc) = 1),
coffees as (
    select roaster_id, count(*) filter (where is_listed) as coffees_listed,
           count(*) filter (where in_stock) as coffees_in_stock
    from {{ ref('dim_coffee') }} group by roaster_id
)
select
    r.roaster_id, r.roaster_name, r.city, r.state, r.locations, r.is_chain, r.opted_out, r.sources,
    coalesce(g.final_host, r.seed_host, r.roaster_id) as host,
    coalesce(g.platform, r.seed_platform) as platform,
    coalesce(cast(lp.probed_at as date), r.seed_probed) as last_probed,
    lo.run_date as last_ok_run, rc.last_fetch,
    case
        when r.opted_out then 'opted_out'
        when rc.attempts >= {{ var('max_failures') }} and rc.errors = rc.attempts
             and (lp.probed_at is null or lp.probed_at < rc.last_fetch or lp.status <> 'ok') then 'unreachable'
        when coalesce(g.platform, r.seed_platform) is null
             or coalesce(g.platform, r.seed_platform) like 'http\_%' escape '\' then 'unreachable'
        when lo.roaster_id is null then 'pending'
        when lo.coffees > 0 then 'active'
        when lo.products > 0 then 'no_coffee'
        else 'no_catalog'
    end as status,
    coalesce(c.coffees_listed, 0) as coffees_listed, coalesce(c.coffees_in_stock, 0) as coffees_in_stock
from {{ ref('stg_registry') }} r
left join last_probe lp using (roaster_id)
left join last_good_probe g using (roaster_id)
left join recent rc using (roaster_id)
left join last_ok lo using (roaster_id)
left join coffees c using (roaster_id)
