-- One row per distinct input of the bag-weight rules (stg_snapshots.grams_key) among each coffee's latest run (the
-- rows dim_coffee reads; older weeks' grams are never used, so they aren't recomputed): the product title, its
-- description, tags and URL handle, and each option's title, price as the store states it, and the grams the
-- crawler stored, in the snapshot's order. stg_variant_grams runs coffeedata.bag_weight over these on every build.
with latest as (
    select * from {{ ref('stg_snapshots') }}
    qualify run_date = max(run_date) over (partition by coffee_id)
)
select distinct on (s.grams_key) s.grams_key, s.title, i.body_text, s.tags, s.handle,
       list_transform(s.variants, lambda v: struct_pack(title := v.title, price := v.price, grams := v.grams))
           as variants
from latest s
join {{ ref('stg_product_inputs') }} i using (input_hash)
