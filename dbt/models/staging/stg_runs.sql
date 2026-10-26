-- One row per successful roaster fetch run: the batch that counts for that (roaster, run_date) is the latest
-- successful one, so re-running a week never double-counts. prev/next_run give each roaster's run sequence.
with ok as (
    select roaster_id, run_date, arg_max(batch_id, fetched_at) as batch_id, max(fetched_at) as fetched_at
    from {{ ref('stg_fetches') }}
    where status = 'ok'
    group by roaster_id, run_date
)
select
    *,
    lag(run_date) over w as prev_run,
    lead(run_date) over w as next_run,
    run_date = max(run_date) over (partition by roaster_id) as is_latest_run
from ok
window w as (partition by roaster_id order by run_date)
