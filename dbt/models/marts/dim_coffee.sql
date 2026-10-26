with prices as (
    select
        coffee_id,
        bool_or(available) as in_stock,
        min(price_usd) filter (where price_usd > 0) as min_price_usd,
        min(price_per_100g) filter (where available) as price_per_100g,
        arg_min(grams, price_per_100g) filter (where available) as best_value_grams,
        list(distinct grams order by grams) filter (where grams is not null) as bag_sizes_g
    from {{ ref('stg_variants') }}
    group by coffee_id
),
notes as (
    select e.roaster_slug, e.product_id,
           list(distinct f.category order by f.category) filter (where f.category is not null) as flavor_categories
    from {{ ref('stg_enriched') }} e
    left join lateral (select unnest(e.tasting_notes) as note) n on true
    left join {{ ref('flavor_lexicon') }} f on f.note = n.note
    group by all
)
select
    c.coffee_id, c.roaster_slug, r.roaster_name, r.state as roaster_state, r.is_chain, c.title, c.url, c.image,
    c.platform, c.first_seen,
    e.origin_country, e.origin_region, e.producer, e.process, e.varietals, e.elevation_masl_min,
    e.elevation_masl_max, e.roast_level, e.tasting_notes, coalesce(n.flavor_categories, []) as flavor_categories,
    coalesce(e.is_blend, false) as is_blend, coalesce(e.is_decaf, false) as is_decaf, e.enrichment_method,
    coalesce(p.in_stock, false) as in_stock, p.min_price_usd, p.price_per_100g, p.best_value_grams, p.bag_sizes_g
from {{ ref('stg_catalog') }} c
left join {{ ref('stg_roasters') }} r using (roaster_slug)
left join {{ ref('stg_enriched') }} e using (roaster_slug, product_id)
left join notes n using (roaster_slug, product_id)
left join prices p using (coffee_id)
