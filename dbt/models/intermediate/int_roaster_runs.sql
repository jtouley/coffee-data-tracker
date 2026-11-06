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
    bool_or(status = 'crashed') and not bool_or(status = 'ok') as quarantined,
    -- every ok attempt read only part of the catalog (no complete one this run), and why (the latest's reason)
    bool_or(status = 'ok') and not bool_or(status = 'ok' and not partial) as partial,
    case when bool_or(status = 'ok') and not bool_or(status = 'ok' and not partial)
        then arg_max(error, fetched_at) filter (where status = 'ok' and partial) end as partial_reason,
    -- no ok attempt, and an edge answered a failed one with a challenge: which edge (the latest challenged attempt's)
    case when not bool_or(status = 'ok')
        then arg_max(challenge, fetched_at) filter (where challenge is not null) end as challenge
from {{ ref('stg_fetches') }}
group by roaster_id, run_date
