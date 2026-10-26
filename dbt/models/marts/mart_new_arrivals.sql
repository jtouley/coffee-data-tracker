select coffee_id, roaster_name, title, origin_country, process, tasting_notes, price_per_100g, min_price_usd, url, first_seen
from {{ ref('dim_coffee') }}
where in_stock and first_seen >= current_date - 30
order by first_seen desc, roaster_name, title
