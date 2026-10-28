-- This build's catalog in a few numbers. Each build appends them to the lake's builds table, and the next build's
-- catalog_volume_vs_last_build test compares against the served release. Listed means the current catalog, as
-- on the site: a roaster's archived back catalog appearing or growing is not a swing in the market, and wholesale
-- listings (a roaster's wholesale twin of a retail coffee) aren't counted as more coffees.
with q as (select * from {{ ref('stg_run_quality') }}),
latest as (select max(run_date) as run_date from q),
current_catalog as (
    select * from {{ ref('dim_coffee') }} where is_listed and catalog_status <> 'archived' and not is_wholesale
)
select 'listed_coffees' as metric, count(*)::double as value from current_catalog
union all
select 'in_stock_coffees', count(*)::double from {{ ref('dim_coffee') }} where in_stock and not is_wholesale
union all
select 'roasters_with_coffee', count(distinct roaster_id)::double from current_catalog
union all
select 'suspect_runs_latest_week', count(*)::double from q, latest
where q.run_date = latest.run_date and q.suspect and q.override_action is null
union all
select 'run_roasters_expected', count(*)::double from {{ ref('run_completeness') }}
union all
select 'run_attempted_share', coalesce(avg(attempted::int), 0)::double from {{ ref('run_completeness') }}
union all
select 'run_ok_share', coalesce(avg(ok::int), 0)::double from {{ ref('run_completeness') }}
