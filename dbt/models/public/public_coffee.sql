-- Facts only: names, origin and process attributes, sizes, prices, stock, and a link back to the roaster.
-- Deliberately excluded: product descriptions, images, store-internal ids/handles, and anything from the
-- personal shot log.
select
    c.coffee_id, c.roaster_id, r.roaster_name, r.state as roaster_state,
    c.title as coffee_name, c.url as source_url,
    c.is_listed, c.in_stock, c.catalog_status, c.published_at, c.first_seen, c.last_seen, c.weeks_listed,
    c.origin_country, c.origin_region, c.process, c.varietals, c.elevation_masl_min, c.elevation_masl_max,
    c.roast_level, c.tasting_notes, c.flavor_categories, c.is_blend, c.is_decaf,
    c.min_price_usd, c.price_per_100g, c.bag_sizes_g,
    c.enrichment_method as attributes_source,
    {{ release_columns() }}
from {{ ref('dim_coffee') }} c
join {{ ref('public_roaster') }} r using (roaster_id)
where c.url is not null  -- every published coffee links back to its roaster; dim_coffee drops unsafe links
