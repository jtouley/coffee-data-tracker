{{ config(severity='warn') }}
-- This week's fetches held out of history as suspect: review each, then accept or reject it with
-- `coffeedata override`. A warning, not an error: the history already treats them as missing weeks.
select roaster_id, run_date, batch_id, products, baseline_products
from {{ ref('stg_run_quality') }}
where suspect and override_action is null
  and run_date = (select max(run_date) from {{ ref('stg_run_quality') }})
