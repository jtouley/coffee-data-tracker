-- Every product version in the lake whose tags decoding changes, with both forms: the raw tags, which the Python
-- readers of the lake (registry.py, enrich.py, verdict.py) see, and the decoded tags, which the build sees
-- (stg_snapshots, int_fetch_product_inputs). One row per (roaster, product, version). int_tag_decoding_parity runs
-- the rules over both. Only tags holding a '&' can change, so the rest of the lake is filtered out first.
with s as (
    select distinct on (roaster_id, product_id, input_hash) * from (
        select s.roaster_id, s.product_id, {{ product_input_hash('s') }} as input_hash, s.title, s.product_type,
               s.tags as raw_tags, s.body_hash, list_transform(s.variants, lambda v: v.grams) as grams
        from {{ lake('snapshots') }} s
        where list_bool_or(list_transform(s.tags, lambda t: contains(t, '&')))
    )
),
d as (select *, {{ decode_html_entities_list('raw_tags') }} as tags from s),
b as (select body_hash, any_value(body_text) as body_text from {{ lake('bodies') }} group by body_hash)
select d.roaster_id, d.product_id, d.input_hash, d.title, d.product_type, d.raw_tags, d.tags, d.body_hash,
       d.grams, b.body_text
from d
left join b using (body_hash)
where d.tags is distinct from d.raw_tags
