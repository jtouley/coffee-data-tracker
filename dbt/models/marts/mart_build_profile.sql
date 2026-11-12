-- This build's catalog and run in a few numbers. Each build appends them to the lake's builds table: the next
-- build's catalog_volume_vs_last_build test compares the volume metrics with the served build's, and
-- build_metrics_in_band compares the volume metrics and the ok shares, and fill_and_freshness_in_band the filled
-- shares and the share seen this run, with the recent passing builds' (that is how their history accumulates).
-- Listed means the current catalog, as on the site: a roaster's archived back catalog appearing or growing is not a
-- swing in the market, and wholesale listings (a roaster's wholesale twin of a retail coffee) aren't counted as more
-- coffees.
--   run_catalog_ok_share: the share of the run's expected roasters that have a catalog (run_completeness
--     has_catalog) that it fetched with products; adapter_ok_share:<adapter> is the same per adapter, and
--     adapter_roasters:<adapter> how many such roasters each adapter has.
--   filled_share:<column> (origin_country, process, roast_level, price_per_100g): the share of listed coffees with
--     that column filled in. A break in extraction or enrichment (Claude down, a prompt or rules regression) leaves
--     nulls, which accepted_values lets through.
--   listed_seen_this_run_share: the share of listed coffees last seen in this run (as_of_run, else the latest
--     counted run). A partial fetch keeps its unread products listed, so this is below 1 in a normal week.
--   run_quarantined: the run's roasters quarantined after crashing their crawl twice (fetch status crashed), which
--     the ok shares leave out; not banded.
--   Per adapter, over its roasters with a catalog (adapter_volume_in_band bands them, low side only):
--     adapter_attempted_ok_share:<adapter>: catalog_ok among the roasters the run tried. Unlike adapter_ok_share
--       it doesn't fall when a run simply tries fewer roasters (run_is_complete judges that), only when the
--       platform's fetches fail.
--     adapter_same_store_products:<adapter>: products this run over products in each roaster's previous fetch that
--       brought products, over the roasters fetched with products both times: a platform whose fetches "succeed"
--       with fewer products (a challenge page, a truncated listing) shows as a drop below 1.
--     adapter_same_store_priced:<adapter>: the same ratio for products with a priced variant: a price wipe on one
--       platform. Both ratios leave out roasters whose fetch failed (the ok shares count those).
--   accepted_volume_change (1: this build was let through with --accept-volume-change) and accepted_overrides
--     (accept overrides in the lake when it was built): where the bands' history starts again.
with q as (select * from {{ ref('stg_run_quality') }}),
latest as (select max(run_date) as run_date from q),
catalog as (select * from {{ ref('run_completeness') }} where has_catalog),
listed as (select * from {{ ref('int_current_catalog') }} where not is_wholesale),
this_run as (
    select coalesce(cast(nullif('{{ var("as_of_run") }}', '') as date),
                    (select max(run_date) from {{ ref('stg_runs') }})) as run_date
),
-- Per roaster and run: the products a successful fetch brought, and how many of them had a priced variant (null
-- when the week has no snapshots, e.g. a rejected roaster-week, so it says nothing about prices).
fetched as (
    select r.roaster_id, r.run_date, r.products, s.priced
    from {{ ref('int_roaster_runs') }} r
    left join (
        select roaster_id, run_date,
               count(distinct product_id) filter (where list_bool_or(list_transform(variants,
                   lambda v: coalesce(v.price_usd > 0, false)))) as priced
        from {{ ref('stg_snapshots') }} group by all
    ) s using (roaster_id, run_date)
    where r.ok and r.products > 0
),
same_store as (
    select c.adapter, f.products, f.priced, p.products as prev_products, p.priced as prev_priced
    from catalog c
    join fetched f on f.roaster_id = c.roaster_id and f.run_date = c.run_date
    join lateral (
        select products, priced from fetched e
        where e.roaster_id = c.roaster_id and e.run_date < c.run_date order by e.run_date desc limit 1
    ) p on true
    where c.catalog_ok
)
select 'listed_coffees' as metric, count(*)::double as value from {{ ref('int_current_catalog') }} where not is_wholesale
union all
select 'in_stock_coffees', count(*)::double from {{ ref('dim_coffee') }} where in_stock and not is_wholesale
union all
select 'roasters_with_coffee', count(distinct roaster_id)::double from {{ ref('int_current_catalog') }}
where not is_wholesale
union all
select 'suspect_runs_latest_week', count(*)::double from q, latest
where q.run_date = latest.run_date and q.suspect and q.override_action is null
union all
select 'run_roasters_expected', count(*)::double from {{ ref('run_completeness') }}
union all
select 'run_attempted_share', coalesce(avg(attempted::int), 0)::double from {{ ref('run_completeness') }}
union all
select 'run_ok_share', coalesce(avg(ok::int), 0)::double from {{ ref('run_completeness') }}
union all
select 'run_quarantined', count(*) filter (where quarantined)::double from {{ ref('run_completeness') }}
union all
select 'run_catalog_roasters', count(*)::double from catalog
union all
select 'run_catalog_ok_share', coalesce(avg(catalog_ok::int), 0)::double from catalog
union all
select 'adapter_roasters:' || adapter, count(*)::double from catalog group by adapter
union all
select 'adapter_ok_share:' || adapter, avg(catalog_ok::int)::double from catalog group by adapter
union all
select 'adapter_attempted_ok_share:' || adapter, avg(catalog_ok::int)::double from catalog where attempted
group by adapter
union all
select 'adapter_same_store_products:' || adapter, (sum(products) / sum(prev_products))::double from same_store
group by adapter
union all
select 'adapter_same_store_priced:' || adapter, (sum(priced) / sum(prev_priced))::double
from same_store where prev_priced > 0 and priced is not null group by adapter
union all
{%- for column in ['origin_country', 'process', 'roast_level', 'price_per_100g'] %}
select 'filled_share:{{ column }}', coalesce(avg(({{ column }} is not null)::int), 0)::double from listed
union all
{%- endfor %}
select 'listed_seen_this_run_share', coalesce(avg((listed.last_seen = this_run.run_date)::int), 0)::double
from listed, this_run
union all
select 'accepted_volume_change', {{ 1 if var('accept_volume_change') else 0 }}::double
union all
select 'accepted_overrides', count(*)::double from {{ lake('run_overrides') }}
where roaster_id is not null and action = 'accept'
