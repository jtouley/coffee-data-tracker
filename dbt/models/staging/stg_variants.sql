with v as (select coffee_id, roaster_slug, product_id, unnest(variants) as v from {{ ref('stg_catalog') }})
select
    coffee_id, roaster_slug, product_id, v.variant_id, v.title as variant_title, v.price_usd, v.grams,
    coalesce(v.available, true) as available,
    case when v.grams between 150 and 2500 and v.price_usd > 0 then round(v.price_usd / v.grams * 100, 2) end
        as price_per_100g
from v
