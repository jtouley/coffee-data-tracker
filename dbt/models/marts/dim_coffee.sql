-- Every coffee ever seen, with its most recent listing. Delisted coffees stay (is_listed = false), so price
-- history and shots that reference them keep resolving.
with seen as (
    select coffee_id, min(run_date) as first_seen, max(run_date) as last_seen, count(*) as weeks_listed
    from {{ ref('stg_snapshots') }} where is_coffee group by coffee_id
),
-- The latest record decides: a product the classifier no longer considers coffee drops out entirely.
latest as (
    select * from (
        select * from {{ ref('stg_snapshots') }}
        qualify row_number() over (partition by coffee_id order by run_date desc) = 1
    ) where is_coffee
),
variants as (
    select coffee_id, v.* from latest, unnest(variants) as t(v)
),
prices as (
    select
        coffee_id,
        bool_or(coalesce(available, true)) as any_available,
        min(price_usd) filter (where price_usd > 0) as min_price_usd,
        -- A price outside $0.50-$150 per 100 g is a misread weight or price, not a real bag.
        min(round(price_usd / grams * 100, 2)) filter (where grams between 150 and 2500 and price_usd > 0
                                                         and coalesce(available, true)
                                                         and price_usd / grams * 100 between 0.5 and 150)
            as price_per_100g,
        list(distinct grams order by grams) filter (where grams is not null) as bag_sizes_g
    from variants group by coffee_id
),
notes as (
    select e.roaster_id, e.product_id, e.body_hash,
           list(distinct f.category order by f.category) filter (where f.category is not null) as flavor_categories
    from {{ ref('stg_enrichments') }} e
    left join lateral (select unnest(e.tasting_notes) as note) n on true
    left join {{ ref('flavor_lexicon') }} f on f.note = n.note
    group by all
)
select
    l.coffee_id, l.roaster_id, r.roaster_name, r.state as roaster_state, r.is_chain, l.product_id, l.handle,
    -- Links come from roasters' feeds; keep only plain http(s) ones (no javascript:, data:, ...).
    l.title, case when regexp_full_match(l.url, 'https?://[^\s"<>]+') then l.url end as url, l.image, l.platform, s.first_seen, s.last_seen, s.weeks_listed,
    run.is_latest_run as is_listed,
    e.origin_country, e.origin_region, e.producer, e.process, e.varietals, e.elevation_masl_min,
    e.elevation_masl_max, e.roast_level, e.tasting_notes, coalesce(n.flavor_categories, []) as flavor_categories,
    coalesce(e.is_blend, false) as is_blend, coalesce(e.is_decaf, false) as is_decaf,
    e.method as enrichment_method,
    run.is_latest_run and coalesce(p.any_available, false) as in_stock,
    p.min_price_usd, p.price_per_100g, p.bag_sizes_g
from latest l
join seen s using (coffee_id)
join {{ ref('stg_runs') }} run using (roaster_id, run_date)
left join {{ ref('stg_registry') }} r using (roaster_id)
left join {{ ref('stg_enrichments') }} e using (roaster_id, product_id, body_hash)
left join notes n using (roaster_id, product_id, body_hash)
left join prices p using (coffee_id)
