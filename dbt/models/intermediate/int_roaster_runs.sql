-- One row per roaster and weekly run that tried it: whether the run fetched it (any attempt succeeded, so a re-run
-- that fixed a failure counts) and when its attempts ran. Failure streaks (dim_roaster) and run completeness
-- (run_completeness) count runs, not attempts.
select roaster_id, run_date, bool_or(status = 'ok') as ok, min(fetched_at) as first_at, max(fetched_at) as last_at
from {{ ref('stg_fetches') }}
group by roaster_id, run_date
