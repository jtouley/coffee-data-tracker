select s.roaster_id || ':' || s.product_id as coffee_id, s.* exclude (batch_id, fetched_at), r.fetched_at
from {{ lake('snapshots') }} s
join {{ ref('stg_runs') }} r using (roaster_id, run_date, batch_id)
