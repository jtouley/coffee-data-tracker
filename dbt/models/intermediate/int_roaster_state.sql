-- Current roaster state, derived from probe and fetch history, and each roaster's store (canonical_roaster_id).
-- Mirrors registry.STATE_SQL in Python, which picks what to fetch; tests/test_dbt.py checks the two agree on every
-- roaster's status and canonical id. Upstream of dim_coffee (which leaves aliases out), so it reads no coffee table:
-- dim_roaster adds the coffee counts.
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
           count(*) filter (where rn <= {{ var('max_failures') }} and not ok) as errors, max(last_at) as last_fetch,
           -- the first run that read a catalog (history: it only ever moves earlier), for the canonical id
           min(run_date) filter (where ok and products > 0) as first_catalog_run
    from runs group by roaster_id
),
-- The chain overrides: the build's snapshot's chain flags (var chain_flags, the admin's Sources page, which
-- `coffeedata build` passes once a published snapshot carries them), else seeds/chain_overrides.csv. A flag decides
-- over the store count, either way; a roaster with none is decided by its count.
{%- set chain_flags = var('chain_flags', none) %}
chain_overrides as (
{%- if chain_flags is none %}
    select roaster_id, is_chain from {{ ref('chain_overrides') }}
{%- else %}
    select null::varchar as roaster_id, null::boolean as is_chain where false
    {%- for f in chain_flags %}
    union all select '{{ f.roaster_id | replace("'", "''") }}', {{ 'true' if f.chain else 'false' }}
    {%- endfor %}
{%- endif %}
),
last_ok as (
    select * from {{ ref('int_fetches') }} where status = 'ok'
    qualify row_number() over (partition by roaster_id order by fetched_at desc, batch_id desc) = 1
),
base as (
select
    r.roaster_id, r.roaster_name, r.city, r.state, r.country, r.locations,
    -- A chain: more than chain_store_threshold stores (locations), unless an override (chain_overrides) says otherwise.
    -- Still crawled and kept in the internal marts; public_roaster leaves a chain out of every public output.
    coalesce(o.is_chain, r.locations > {{ var('chain_store_threshold') }}) as is_chain,
    r.opted_out, r.sources,
    r.alias_of, r.first_added, rc.first_catalog_run,
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
    end as crawl_status
from {{ ref('stg_registry') }} r
left join chain_overrides o using (roaster_id)
left join last_probe lp using (roaster_id)
left join last_good_probe g using (roaster_id)
left join recent rc using (roaster_id)
left join last_ok lo using (roaster_id)
),
shared_hosts as (select host from {{ ref('shared_hosts') }}),
-- One store, one roaster_id: the rule and its reasons are in macros/roaster_alias.sql (registry.STATE_SQL renders
-- the same macro).
canonical as (
    {{ roaster_canonical('base', 'shared_hosts') }}
)
select
    b.* exclude (crawl_status), b.crawl_status,
    -- alias: another roaster_id serves the same store (canonical_roaster_id). Never crawled, never in dim_coffee or
    -- the public tables. An opted-out id keeps opted_out.
    case when b.crawl_status <> 'opted_out' and c.canonical_roaster_id <> b.roaster_id then 'alias'
         else b.crawl_status end as status,
    c.canonical_roaster_id
from base b
join canonical c using (roaster_id)
