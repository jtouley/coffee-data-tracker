-- Current roaster state, derived from probe and fetch history (mirrors registry.STATE_SQL in Python, which
-- picks what to fetch; tests/test_dbt.py::test_python_and_dbt_agree_on_roaster_status checks the two agree).
-- A probe whose robots.txt didn't answer is no answer about the site: it doesn't count as probed (as in Python).
-- Each "latest" pick breaks a tie in time by the values it reads, so the same lake always builds the same table.
with probes as (select * from {{ ref('stg_probes') }} where status <> 'robots_unavailable'),
last_probe as (
    select * from probes
    qualify row_number() over (partition by roaster_id order by probed_at desc, status, final_host, platform) = 1
),
last_good_probe as (
    select * from probes where status = 'ok'
    qualify row_number() over (partition by roaster_id order by probed_at desc, final_host, platform) = 1
),
-- Failure streaks count runs, not attempts.
runs as (
    select *, row_number() over (partition by roaster_id order by run_date desc) as rn
    from {{ ref('int_roaster_runs') }}
),
recent as (
    select roaster_id, count(*) filter (where rn <= {{ var('max_failures') }}) as attempts,
           count(*) filter (where rn <= {{ var('max_failures') }} and not ok) as errors, max(last_at) as last_fetch
    from runs group by roaster_id
),
last_ok as (
    select * from {{ ref('int_fetches') }} where status = 'ok'
    qualify row_number() over (partition by roaster_id order by fetched_at desc, batch_id desc) = 1
),
coffees as (
    -- Listed means the current catalog; a roaster's archived back catalog is counted separately.
    -- Wholesale listings (a roaster's wholesale twin of a retail coffee) aren't counted as more coffees.
    select roaster_id,
           count(*) filter (where is_listed and catalog_status <> 'archived' and not is_wholesale) as coffees_listed,
           count(*) filter (where in_stock and not is_wholesale) as coffees_in_stock,
           count(*) filter (where catalog_status = 'archived' and not is_wholesale) as coffees_archived
    from {{ ref('dim_coffee') }} group by roaster_id
),
base as (
select
    r.roaster_id, r.roaster_name, r.city, r.state, r.country, r.locations, r.is_chain, r.opted_out, r.sources,
    r.alias_of, r.first_added,
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
    coalesce(c.coffees_listed, 0) as coffees_listed, coalesce(c.coffees_in_stock, 0) as coffees_in_stock,
    coalesce(c.coffees_archived, 0) as coffees_archived
from {{ ref('stg_registry') }} r
left join last_probe lp using (roaster_id)
left join last_good_probe g using (roaster_id)
left join recent rc using (roaster_id)
left join last_ok lo using (roaster_id)
left join coffees c using (roaster_id)
),
-- One store, one roaster_id (registry.STATE_SQL decides the same way, so the crawl skips what dbt calls an alias).
-- Roasters whose resolved hosts have the same key (macro host_key) serve the same store. Among those the crawl
-- reads or is about to read (active or pending), the canonical id is, in order: one already read (active), so a
-- domain discovery adds later never takes over an id already published; the id equal to the key; one whose first
-- label is the key's (unionroasters.co for unionroasters.com); a curated one; the earliest added; the first by
-- name. A hand-checked alias_of in config/roasters.csv overrides the host (a rebrand whose old domain doesn't
-- redirect). Other statuses never group: a parked or dead domain (hugedomains.com, bitly.com) is many roasters'
-- host without being their store.
host_groups as (
    select roaster_id,
           first_value(roaster_id) over (
               partition by {{ host_key('host') }}
               order by status = 'active' desc, roaster_id = {{ host_key('host') }} desc,
                        split_part(roaster_id, '.', 1) = split_part({{ host_key('host') }}, '.', 1) desc,
                        regexp_matches(coalesce(sources, ''), '(^|\+)curated') desc, first_added asc nulls last,
                        roaster_id
           ) as host_canonical
    from base
    where status in ('active', 'pending') and alias_of is null
)
select
    b.roaster_id, b.roaster_name, b.city, b.state, b.country, b.locations, b.is_chain, b.opted_out, b.sources,
    b.host, b.platform, b.last_probed, b.last_ok_run, b.last_fetch,
    -- alias: another roaster_id serves the same store (canonical_roaster_id). Never crawled, never public.
    case when b.status <> 'opted_out' and coalesce(b.alias_of, h.host_canonical, b.roaster_id) <> b.roaster_id
         then 'alias' else b.status end as status,
    coalesce(b.alias_of, h.host_canonical, b.roaster_id) as canonical_roaster_id,
    b.coffees_listed, b.coffees_in_stock, b.coffees_archived,
    {{ release_columns() }}
from base b
left join host_groups h using (roaster_id)
