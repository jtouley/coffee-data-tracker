-- This build's catalog and run in a few numbers. Each build appends them to the lake's builds table: the next
-- build's catalog_volume_vs_last_build test compares the volume metrics with the served build's, and
-- build_metrics_in_band compares the volume metrics and the ok shares with the recent passing builds' (that is how
-- their history accumulates). Listed means the current catalog, as on the site: a roaster's archived back catalog
-- appearing or growing is not a swing in the market.
--   run_catalog_ok_share: the share of the run's expected roasters that have a catalog (run_completeness
--     has_catalog) that it fetched with products; adapter_ok_share:<adapter> is the same per adapter, and
--     adapter_roasters:<adapter> how many such roasters each adapter has.
--   accepted_volume_change (1: this build was let through with --accept-volume-change) and accepted_overrides
--     (accept overrides in the lake when it was built): where the bands' history starts again.
with q as (select * from {{ ref('stg_run_quality') }}),
latest as (select max(run_date) as run_date from q),
catalog as (select * from {{ ref('run_completeness') }} where has_catalog)
select 'listed_coffees' as metric, count(*)::double as value from {{ ref('int_current_catalog') }}
union all
select 'in_stock_coffees', count(*)::double from {{ ref('dim_coffee') }} where in_stock
union all
select 'roasters_with_coffee', count(distinct roaster_id)::double from {{ ref('int_current_catalog') }}
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
select 'run_catalog_roasters', count(*)::double from catalog
union all
select 'run_catalog_ok_share', coalesce(avg(catalog_ok::int), 0)::double from catalog
union all
select 'adapter_roasters:' || adapter, count(*)::double from catalog group by adapter
union all
select 'adapter_ok_share:' || adapter, avg(catalog_ok::int)::double from catalog group by adapter
union all
select 'accepted_volume_change', {{ 1 if var('accept_volume_change') else 0 }}::double
union all
select 'accepted_overrides', count(*)::double from {{ lake('run_overrides') }}
where roaster_id is not null and action = 'accept'
