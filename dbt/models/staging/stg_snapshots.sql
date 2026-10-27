-- Older generic-adapter snapshots can repeat one variant_id within a product (stores that reuse a SKU across
-- offers). Repeats get the same '<id>#<index>' suffix the adapter now writes, so variant ids are unique.
select s.roaster_id || ':' || s.product_id as coffee_id,
       s.* exclude (batch_id, fetched_at) replace (
           list_transform(s.variants, lambda v, i: struct_update(v, variant_id :=
               case when list_contains(list_transform(s.variants[:i - 1], lambda w: w.variant_id), v.variant_id)
                    then v.variant_id || '#' || (i - 1) else v.variant_id end)) as variants),
       r.fetched_at
from {{ lake('snapshots') }} s
join {{ ref('stg_runs') }} r using (roaster_id, run_date, batch_id)
