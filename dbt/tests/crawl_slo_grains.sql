-- Warn only, and report only (tag report_only): the crawl success SLO is a benchmark (#96), so `coffeedata build`
-- runs this after the gate and it can never hold back a release. It stays a warning for good: there is no plan to
-- make the measurement block.
-- The grain of the SLO's two tables, each key column compared on its own: one row per run and roaster
-- (int_crawl_slo_roasters), one per run and platform (mart_crawl_slo). A row per key that more than one row has,
-- and per row with a null key part. To become the generic `grain` test once the project has it (#157).
{{ config(severity='warn', tags=['report_only']) }}
select 'int_crawl_slo_roasters' as model, run_date, roaster_id as key_part, count(*) as key_rows
from {{ ref('int_crawl_slo_roasters') }}
group by run_date, roaster_id
having count(*) > 1 or run_date is null or roaster_id is null
union all
select 'mart_crawl_slo', run_date, platform, count(*)
from {{ ref('mart_crawl_slo') }}
group by run_date, platform
having count(*) > 1 or run_date is null or platform is null
