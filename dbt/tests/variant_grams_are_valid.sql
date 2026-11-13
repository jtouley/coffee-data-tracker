-- stg_variant_grams gives every coffee's latest version one weight per option, each null or 30 g to 25 kg
-- (coffeedata.bag_weight's range). dim_coffee joins them by position, so a missing row or a short list would
-- silently leave a coffee on its stored grams or an option without a weight.
with latest as (
    select distinct grams_key, len(variants) as options from {{ ref('stg_snapshots') }}
    qualify run_date = max(run_date) over (partition by coffee_id)
)
select l.grams_key, l.options, g.options as rule_options, g.grams, 'no row' as problem
from latest l left join {{ ref('stg_variant_grams') }} g using (grams_key)
where g.grams_key is null
union all
select g.grams_key, l.options, g.options, g.grams, 'length' as problem
from latest l join {{ ref('stg_variant_grams') }} g using (grams_key)
where len(g.grams) <> l.options or g.options <> l.options
union all
select grams_key, options, options, grams, 'range' as problem
from {{ ref('stg_variant_grams') }}
where list_bool_or(list_transform(grams, lambda x: x < 30 or x > 25000))
