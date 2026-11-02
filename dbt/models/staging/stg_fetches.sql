-- status: ok, error, unavailable (a closed, deactivated or password-protected store), or crashed (not fetched:
-- quarantined for the week after it was in flight in two attempts of its crawl that died). partial: an ok fetch
-- that read only part of the catalog (null in files from before raw contract 1.2.0: complete).
select run_date, batch_id, fetched_at, roaster_id, adapter, status, error, products, coffees,
    coalesce(partial, false) as partial
from {{ lake('fetches') }}
where roaster_id is not null
