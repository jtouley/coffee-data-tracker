select run_date, probed_at, roaster_id, status, http_status, final_host, platform
from {{ lake('probes') }}
where roaster_id is not null
