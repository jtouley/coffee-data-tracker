-- One row per roaster and weekly run that tried it: whether the run fetched it (any attempt succeeded, so a re-run
-- that fixed a failure counts), when its attempts ran, the adapter of its latest attempt, and the most products a
-- successful attempt returned (null: none succeeded, or older files without the count). Failure streaks
-- (dim_roaster) and run completeness (run_completeness) count runs, not attempts.
select
    roaster_id, run_date, bool_or(status = 'ok') as ok, min(fetched_at) as first_at, max(fetched_at) as last_at,
    arg_max(adapter, fetched_at) as adapter, max(products) filter (where status = 'ok') as products
from {{ ref('stg_fetches') }}
group by roaster_id, run_date
