-- The latest override for each roaster-week (accept, reject or use_batch). Overrides are appended, never edited.
select roaster_id, run_date, action, batch_id, reason, added_at
from {{ lake('run_overrides') }}
where roaster_id is not null
qualify row_number() over (partition by roaster_id, run_date order by added_at desc) = 1
