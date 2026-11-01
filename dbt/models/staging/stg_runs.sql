-- One row per roaster-week that counts in history (stg_run_quality: the latest successful batch, or the one an
-- override pins, and not a suspect run nobody has accepted), so re-running a week never double-counts.
-- prev/next_run give each roaster's run sequence.
-- is_current: a product seen in this run is listed now. That is the latest run, or any run since the latest
-- complete one: a partial run (a catalog read only in part) lists what it read and leaves the products it didn't
-- read as they were, instead of delisting them. Without partial runs it equals is_latest_run.
with counted as (
    select roaster_id, run_date, batch_id, fetched_at, partial from {{ ref('stg_run_quality') }} where counted
)
select
    *,
    lag(run_date) over w as prev_run,
    lead(run_date) over w as next_run,
    run_date = max(run_date) over (partition by roaster_id) as is_latest_run,
    run_date >= coalesce(
        max(run_date) filter (where not partial) over (partition by roaster_id),
        min(run_date) over (partition by roaster_id)
    ) as is_current
from counted
window w as (partition by roaster_id order by run_date)
