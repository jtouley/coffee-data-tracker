-- One row per distinct input of the bag-weight rules (stg_snapshots.grams_key): the product title, its description,
-- and each option's title, price as the store states it, and the grams the crawler stored, in the snapshot's order.
-- stg_variant_grams runs coffeedata.bag_weight over these on every build.
select distinct on (s.grams_key) s.grams_key, s.title, i.body_text,
       list_transform(s.variants, lambda v: struct_pack(title := v.title, price := v.price, grams := v.grams))
           as variants
from {{ ref('stg_snapshots') }} s
join {{ ref('stg_product_inputs') }} i using (input_hash)
