{{ config(severity='warn') }}
-- This week's fetches held out of history as suspect: review each, then accept or reject it with
-- `coffeedata override`. A warning, not an error: the history already treats them as missing weeks.
select roaster_id, run_date, batch_id, products, baseline_products
from {{ ref('stg_run_quality') }}
where suspect and override_action is null
  -- The judged week: as_of_run when set (the build's run, or the week `coffeedata fetch-health` checks, which may have
  -- no ok fetch yet, when the latest week with one is last week's); else the latest week with an ok fetch.
  {% if var('as_of_run') %}
  and run_date = cast('{{ var("as_of_run") }}' as date)
  {% else %}
  and run_date = (select max(run_date) from {{ ref('stg_run_quality') }})
  {% endif %}
