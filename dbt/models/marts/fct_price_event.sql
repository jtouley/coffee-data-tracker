select
    o.observed_at, o.roaster_slug || ':' || c.handle as coffee_id, o.roaster_slug, o.product_id, o.variant_id,
    o.event, o.price_usd, o.available,
    lag(o.price_usd) over (partition by o.roaster_slug, o.product_id, o.variant_id order by o.observed_at)
        as previous_price_usd
from {{ ref('stg_observations') }} o
left join {{ ref('stg_catalog') }} c using (roaster_slug, product_id)
