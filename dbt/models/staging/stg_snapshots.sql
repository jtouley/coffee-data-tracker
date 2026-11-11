-- Older generic-adapter snapshots can repeat one variant_id within a product (stores that reuse a SKU across
-- offers). Repeats get the same '<id>#<index>' suffix the adapter now writes, so variant ids are unique.
-- payload_hash is fetch metadata for the lake (real changes, repeated fetches), not part of any mart.
-- Tags are decoded here (some stores' APIs return them HTML-escaped: "Parts &amp; Equipment"); the lake keeps the
-- raw value, and input_hash stays over it, so stored verdicts and enrichments still join. Each distinct escaped tag
-- is decoded once (html_tag_decodings, a second read of the lake's tags column alone).
-- Prices: the lake keeps each price as the store states it, with its currency (variants.currency, from raw 1.6.0;
-- null in older files and where a store states none). Only USD reaches the marts: a variant's price_usd here is
-- the store's price when it is in USD (is_usd), else null, and `price` keeps the store's own. is_usd: the store
-- states USD, or states no currency and the roaster is in the US (stg_registry.country), which is how every
-- older row of a US roaster reads, as before. A stated non-USD currency, or none from a roaster outside the US (or
-- not in the registry), is never published as dollars.
select s.roaster_id || ':' || s.product_id as coffee_id,
       s.* exclude (batch_id, fetched_at, is_coffee, payload_hash) replace (
           list_transform(s.variants, lambda v, i: struct_update(v, variant_id :=
               case when list_contains(list_transform(s.variants[:i - 1], lambda w: w.variant_id), v.variant_id)
                    then v.variant_id || '#' || (i - 1) else v.variant_id end,
               price := v.price_usd,
               is_usd := {{ is_usd('v.currency', 'g.country') }},
               price_usd := case when {{ is_usd('v.currency', 'g.country') }} then v.price_usd end)) as variants,
           {{ decode_tags_with('s.tags', 'html.tag_map') }} as tags),
       r.fetched_at,
       -- Everything the classifier and the extraction rules read. stg_products runs them once per distinct value.
       {{ product_input_hash('s') }} as input_hash,
       -- Everything the bag-weight rules read (coffeedata.bag_weight). stg_variant_grams runs them once per value.
       {{ variant_grams_key('s') }} as grams_key
from {{ lake('snapshots') }} s
join {{ ref('stg_runs') }} r using (roaster_id, run_date, batch_id)
left join {{ ref('stg_registry') }} g using (roaster_id)
cross join {{ html_tag_decodings(lake('snapshots')) }} html
