-- One row per roaster and weekly run that tried it: whether the run fetched it (any attempt succeeded, so a re-run
-- that fixed a failure counts), when its attempts ran, the adapter of its latest attempt, and the most products a
-- successful attempt returned (null: none succeeded, or older files without the count). Failure streaks
-- (dim_roaster) and run completeness (run_completeness) count runs, not attempts.
select
    roaster_id, run_date, bool_or(status = 'ok') as ok, min(fetched_at) as first_at, max(fetched_at) as last_at,
    arg_max(adapter, fetched_at) as adapter, max(products) filter (where status = 'ok') as products,
    -- closed this run (Shopify's closed-store page, a password page, robots disallowing the whole site) and never
    -- fetched: not a failure of the run, so run_ok_rate and adapter_ok_rate leave it out (run_completeness)
    bool_or(status = 'unavailable') and not bool_or(status = 'ok') as unavailable,
    -- quarantined this run after crashing its crawl twice (crashguard) and never fetched: a problem with that one
    -- store, not the platform's or the run's, so the ok rates leave it out too (run_completeness)
    bool_or(status = 'crashed') and not bool_or(status = 'ok') as quarantined
from {{ ref('stg_fetches') }}
group by roaster_id, run_date
