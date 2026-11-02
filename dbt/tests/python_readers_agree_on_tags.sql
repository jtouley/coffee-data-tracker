-- The Python readers of the lake (registry.py, enrich.py, verdict.py) read raw tags; the build decodes them. Each
-- product where they would decide differently fails the build (int_tag_decoding_parity: one row per product, with
-- its disagreeing versions and the outputs that differ). `coffeedata build` prints them. To fix one, decode tags in
-- coffeedata.products.item as the build does, or change the rule that reads the escaped form.
select roaster_id, product_id, count(*) as versions, any_value(title) as title, any_value(raw_tags) as raw_tags,
       any_value(tags) as tags, list_distinct(flatten(list(differs))) as differs
from {{ ref('int_tag_decoding_parity') }}
group by roaster_id, product_id
