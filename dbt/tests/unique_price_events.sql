-- One event per (coffee, bag size, run, kind): re-running a week must not duplicate history.
select coffee_id, variant_id, run_date, event, count(*)
from {{ ref('fct_price_event') }}
group by all
having count(*) > 1
