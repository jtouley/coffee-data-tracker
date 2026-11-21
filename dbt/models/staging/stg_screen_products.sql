-- One row per product on the one catalog page a screen read (raw screen_products), as the store listed it. A screen
-- lists a product once; a page that repeats an id keeps one row, the same one on every build.
select screen_id, roaster_id, product_id, handle, title, vendor, product_type, tags, variants, body_hash, body_text
from {{ lake('screen_products') }}
where screen_id is not null and product_id is not null
qualify row_number() over (
    partition by screen_id, product_id
    order by handle, title, vendor, product_type, body_hash, body_text
) = 1
