-- Every coffee ever seen, with its most recent listing. Delisted coffees stay (is_listed = false), so price
-- history and shots that reference them keep resolving. Whether a product is coffee, and its attributes, come
-- from stg_products (the current rules over the raw snapshots), so rebuilding from the same lake gives the same
-- table.
with s as (
    select s.*, p.is_coffee from {{ ref('stg_snapshots') }} s join {{ ref('stg_products') }} p using (input_hash)
),
seen as (
    select coffee_id, min(run_date) as first_seen, max(run_date) as last_seen, count(*) as weeks_listed
    from s where is_coffee group by coffee_id
),
-- The latest record decides: a product the classifier no longer considers coffee drops out entirely.
latest as (
    select * from (
        select * from s qualify row_number() over (partition by coffee_id order by run_date desc) = 1
    ) where is_coffee
),
-- Claude's extraction of a description, where there is one, replaces the rules' attributes for it.
llm as (select * from {{ ref('stg_enrichments') }} where method = 'llm'),
attrs as (
    select
        l.coffee_id, p.clean_title, p.is_wholesale,
        if(e.roaster_id is null, p.origin_country, e.origin_country) as origin_country,
        if(e.roaster_id is null, p.origin_region, e.origin_region) as origin_region,
        if(e.roaster_id is null, p.producer, e.producer) as producer,
        if(e.roaster_id is null, p.process, e.process) as process,
        if(e.roaster_id is null, p.varietals, e.varietals) as varietals,
        if(e.roaster_id is null, p.elevation_masl_min, e.elevation_masl_min) as elevation_masl_min,
        if(e.roaster_id is null, p.elevation_masl_max, e.elevation_masl_max) as elevation_masl_max,
        if(e.roaster_id is null, p.roast_level, e.roast_level) as roast_level,
        if(e.roaster_id is null, p.tasting_notes, e.tasting_notes) as tasting_notes,
        if(e.roaster_id is null, p.is_blend, e.is_blend) as is_blend,
        if(e.roaster_id is null, p.is_decaf, e.is_decaf) as is_decaf,
        if(e.roaster_id is null, 'rules', 'llm') as enrichment_method
    from latest l
    join {{ ref('stg_products') }} p using (input_hash)
    left join llm e using (roaster_id, product_id, body_hash)
),
variants as (
    select coffee_id, a.is_wholesale, v.* from latest join attrs a using (coffee_id), unnest(variants) as t(v)
),
prices as (
    select
        coffee_id,
        bool_or(coalesce(available, true)) as any_available,
        min(price_usd) filter (where price_usd > 0) as min_price_usd,
        -- A price outside $0.50-$100 per 100 g is a misread weight or price, not a real bag. Wholesale bags and
        -- cases get no per-weight price, so they stay out of medians and price sorting.
        min(round(price_usd / grams * 100, 2)) filter (where not is_wholesale and grams between 150 and 2500
                                                         and price_usd > 0
                                                         and coalesce(available, true)
                                                         and price_usd / grams * 100 between 0.5 and 100)
            as price_per_100g,
        list(distinct grams order by grams) filter (where grams is not null) as bag_sizes_g
    from variants group by coffee_id
),
notes as (
    select a.coffee_id,
           list(distinct f.category order by f.category) filter (where f.category is not null) as flavor_categories
    from attrs a
    left join lateral (select unnest(a.tasting_notes) as note) n on true
    left join {{ ref('flavor_lexicon') }} f on f.note = n.note
    group by a.coffee_id
)
select
    l.coffee_id, l.roaster_id, r.roaster_name, r.state as roaster_state, r.is_chain, l.product_id, l.handle,
    -- Links come from roasters' feeds; keep only plain http(s) ones (no javascript:, data:, ...).
    a.clean_title as title, case when regexp_full_match(l.url, 'https?://[^\s"<>]+') then l.url end as url,
    l.image, l.platform, s.first_seen, s.last_seen, s.weeks_listed,
    run.is_latest_run as is_listed,
    a.origin_country, a.origin_region, a.producer, a.process, a.varietals, a.elevation_masl_min,
    a.elevation_masl_max, a.roast_level, a.tasting_notes, coalesce(n.flavor_categories, []) as flavor_categories,
    coalesce(a.is_blend, false) as is_blend, coalesce(a.is_decaf, false) as is_decaf,
    a.enrichment_method, a.is_wholesale,
    run.is_latest_run and coalesce(p.any_available, false) as in_stock,
    p.min_price_usd, p.price_per_100g, p.bag_sizes_g
from latest l
join seen s using (coffee_id)
join attrs a using (coffee_id)
join {{ ref('stg_runs') }} run using (roaster_id, run_date)
left join {{ ref('stg_registry') }} r using (roaster_id)
left join notes n using (coffee_id)
left join prices p using (coffee_id)
