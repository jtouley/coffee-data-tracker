-- Coffees that appeared in a roaster's latest run but not in its previous successful run, and that the store
-- published since that run (or doesn't say when). A product we only now see because we fetch more of a catalog
-- (Onyx's back catalog after the pagination fix) is new to us, not a new arrival.
select c.coffee_id, c.roaster_name, c.title, c.origin_country, c.process, c.tasting_notes, c.price_per_100g,
       c.min_price_usd, c.url, c.first_seen
from {{ ref('dim_coffee') }} c
join {{ ref('stg_runs') }} r on r.roaster_id = c.roaster_id and r.is_latest_run
where c.in_stock and c.first_seen = r.run_date and r.prev_run is not null
  and (c.published_at is null or c.published_at >= r.prev_run)
order by c.first_seen desc, c.roaster_name, c.title
