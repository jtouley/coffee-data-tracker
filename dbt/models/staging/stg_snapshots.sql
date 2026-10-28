-- Older generic-adapter snapshots can repeat one variant_id within a product (stores that reuse a SKU across
-- offers). Repeats get the same '<id>#<index>' suffix the adapter now writes, so variant ids are unique.
-- payload_hash is fetch metadata for the lake (real changes, repeated fetches), not part of any mart.
select s.roaster_id || ':' || s.product_id as coffee_id,
       s.* exclude (batch_id, fetched_at, is_coffee, payload_hash) replace (
           list_transform(s.variants, lambda v, i: struct_update(v, variant_id :=
               case when list_contains(list_transform(s.variants[:i - 1], lambda w: w.variant_id), v.variant_id)
                    then v.variant_id || '#' || (i - 1) else v.variant_id end)) as variants),
       r.fetched_at,
       -- Everything the classifier and the extraction rules read. stg_products runs them once per distinct value.
       md5(to_json(struct_pack(s.title, s.product_type, s.tags, s.body_hash,
                               grams := list_transform(s.variants, lambda v: v.grams)))) as input_hash
from {{ lake('snapshots') }} s
join {{ ref('stg_runs') }} r using (roaster_id, run_date, batch_id)
