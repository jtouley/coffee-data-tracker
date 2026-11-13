-- Report only (tag report_only): `coffeedata build` builds it after the gate, so it can never hold back a release.
{{ config(tags=['report_only']) }}
-- The crawl success SLO of the run being built, as metric rows (#96): what the build appends to the lake's builds
-- table beside mart_build_profile's (quality.record_build), for the production health check to read. No build
-- reads them back. Empty when the run being built is not a scored run (int_crawl_slo_roasters), or has none.
--   crawl_slo_roasters[:<platform>], crawl_slo_ok[:<platform>]: the counts; crawl_slo_share[:<platform>] and
--   crawl_slo_met[:<platform>] (1: above the target): the share; crawl_slo_burn_rate, crawl_slo_budget_remaining
--   and crawl_slo_window_runs: the error budget over the window that ends at this run.
select 'crawl_slo_' || m.name || case when s.platform = 'all' then '' else ':' || s.platform end as metric,
       m.value
from {{ ref('mart_crawl_slo') }} s
cross join lateral (values
    ('roasters', s.roasters::double, true), ('ok', s.ok::double, true), ('share', s.sli, true),
    ('met', s.met::int::double, true), ('burn_rate', s.burn_rate, false),
    ('budget_remaining', s.budget_remaining, false), ('window_runs', s.window_runs::double, false)
) m(name, value, per_platform)
where s.is_build_run and (m.per_platform or s.platform = 'all')
