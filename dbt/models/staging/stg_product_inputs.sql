-- One row per distinct product version (title, type, tags, bag sizes, description), with its description text.
-- stg_products runs the current classifier and rules over these on every build.
with v as (
    select distinct on (input_hash) input_hash, title, product_type, tags, body_hash,
           list_transform(variants, lambda x: x.grams) as grams
    from {{ ref('stg_snapshots') }}
),
b as (select body_hash, any_value(body_text) as body_text from {{ lake('bodies') }} group by body_hash)
select v.*, b.body_text from v left join b using (body_hash)
