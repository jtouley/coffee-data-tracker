-- Every roaster-week with a successful fetch: the batch that counts for it, and whether its catalog looks broken
-- (suspect_run, against the median of its last 4 counted weeks). stg_run_walk decides which weeks count; the rule
-- is applied here again, in plain SQL, so dbt unit tests can check it (they can't run a recursive model).
select
    roaster_id, run_date, batch_id, fetched_at, products, coffees,
    list_aggregate(history, 'median') as baseline_products, {{ suspect_run('products', 'history') }} as suspect,
    override_action, override_reason, counted
from {{ ref('stg_run_walk') }}
