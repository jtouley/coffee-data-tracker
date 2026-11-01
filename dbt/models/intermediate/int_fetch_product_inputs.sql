-- The products of every ok fetch whose crawl recorded no coffee count (stg_fetches.coffees is null: an ELT crawl
-- loads without classifying), one row per (fetch, product), with what the crawl's coffee rule reads.
-- int_fetch_coffees counts them. Fetches from before ELT keep the count their crawl recorded, so they aren't here.
with fetches as (
    select roaster_id, batch_id from {{ ref('stg_fetches') }} where status = 'ok' and coffees is null
),
b as (select body_hash, any_value(body_text) as body_text from {{ lake('bodies') }} group by body_hash)
select distinct on (s.roaster_id, s.batch_id, s.product_id)
    s.roaster_id, s.batch_id, s.product_id, {{ product_input_hash('s') }} as input_hash,
    s.title, s.product_type, s.tags, s.body_hash, list_transform(s.variants, lambda v: v.grams) as grams, b.body_text
from {{ lake('snapshots') }} s
join fetches f on s.roaster_id = f.roaster_id and s.batch_id = f.batch_id
left join b using (body_hash)
