-- One row per roaster-week that counts in history (stg_run_quality: the latest successful batch, or the one an
-- override pins, and not a suspect run nobody has accepted), so re-running a week never double-counts.
-- prev/next_run give each roaster's run sequence.
with counted as (
    select roaster_id, run_date, batch_id, fetched_at from {{ ref('stg_run_quality') }} where counted
)
select
    *,
    lag(run_date) over w as prev_run,
    lead(run_date) over w as next_run,
    run_date = max(run_date) over (partition by roaster_id) as is_latest_run
from counted
window w as (partition by roaster_id order by run_date)
