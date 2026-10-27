select run_date, batch_id, fetched_at, roaster_id, adapter, status, error, products, coffees
from {{ lake('fetches') }}
where roaster_id is not null
