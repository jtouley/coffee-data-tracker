-- Report only (tag report_only): `coffeedata build` builds it after the gate, so it can never hold back a release.
{{ config(tags=['report_only']) }}
-- The crawl success SLO (#96): more than crawl_slo_target (99%) of the known roasters fetched ok, as one aggregate
-- over every connector, measured every run. A benchmark, never a gate: nothing here fails a build (the owner's
-- decision, 2026-11-02; the gate's floors are run_ok_rate and adapter_ok_rate). The definition, and which stored
-- runs are scored, are int_crawl_slo_roasters'.
--
-- One row per scored run (up to the run being built) and platform ('all': the SLO itself; a connector: where a
-- miss comes from), counted from int_crawl_slo_roasters: the stored runs alone, never the clock or an earlier
-- build's output, so the same raw gives the same rows and an older run built again gives its numbers again.
-- is_build_run marks the run being built; mart_crawl_slo_profile is its row, as the build records it.
--   sli, sli_low, sli_high: ok / roasters, and its Wilson 95% interval (shares and ratios are rounded to 6 places).
--   met: sli is above the target. burn_rate: (1 - sli) / (1 - target); 1 spends the budget exactly as fast as the
--     target allows, and about crawl_slo_window_runs spends a whole window's budget in one run.
--   window_*: the window is anchored on the row's run, not on a date: that run and the scored runs before it,
--     crawl_slo_window_runs in all (fewer for the first runs), the same runs for every platform. A week with no
--     scored run is not in it (a crawl that never started spends no budget; the release's age reports that).
--     window_budget: the failures the target allows over the window's roasters. budget_remaining: the share of it
--     left, 1 - window_failed / window_budget (below 0: overspent).
{%- set target = var('crawl_slo_target') %}
{%- set z = 1.959964 %}
with counted as (select * from {{ ref('int_crawl_slo_roasters') }} where counted),
run_list as (
    select run_date, bool_or(is_build_run) as is_build_run, row_number() over (order by run_date) as run_no
    from counted
    group by run_date
),
runs as (
    select c.run_date, coalesce(c.platform, 'all') as platform, count(*)::double as roasters,
           (count(*) filter (where c.ok))::double as ok
    from counted c
    group by grouping sets ((c.run_date, c.platform), (c.run_date))
),
windowed as (
    select r.*, l.run_no, l.is_build_run, r.ok / r.roasters as p,
           sum(r.roasters) over w as window_roasters, sum(r.ok) over w as window_ok
    from runs r
    join run_list l using (run_date)
    window w as (partition by r.platform order by l.run_no
                 range between {{ var('crawl_slo_window_runs') - 1 }} preceding and current row)
)
select
    run_date, platform, roasters::bigint as roasters, ok::bigint as ok, (roasters - ok)::bigint as failed,
    round(p, 6) as sli,
    {%- for name, sign in [('sli_low', '-'), ('sli_high', '+')] %}
    round((p + {{ z * z }} / (2 * roasters)
           {{ sign }} {{ z }} * sqrt(p * (1 - p) / roasters + {{ z * z }} / (4 * roasters * roasters)))
          / (1 + {{ z * z }} / roasters), 6) as {{ name }},
    {%- endfor %}
    {{ target }}::double as target,
    p > {{ target }} as met,
    round((roasters - ok) / ((1 - {{ target }}) * roasters), 6) as burn_rate,
    least(run_no, {{ var('crawl_slo_window_runs') }})::bigint as window_runs,
    window_roasters::bigint as window_roasters,
    (window_roasters - window_ok)::bigint as window_failed,
    round(window_ok / window_roasters, 6) as window_sli,
    round((1 - {{ target }}) * window_roasters, 6) as window_budget,
    round(1 - (window_roasters - window_ok) / ((1 - {{ target }}) * window_roasters), 6) as budget_remaining,
    is_build_run
from windowed
