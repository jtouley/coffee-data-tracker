select
    roaster_slug, product_id, handle, roaster_slug || ':' || handle as coffee_id, url, title, platform,
    vendor, product_type, tags, image, variants, body_hash, first_seen
from read_json(
    '{{ var("data_dir") }}/catalog/*.json',
    format = 'array',
    columns = {
        roaster_slug: 'VARCHAR', product_id: 'VARCHAR', handle: 'VARCHAR', url: 'VARCHAR', title: 'VARCHAR',
        platform: 'VARCHAR', vendor: 'VARCHAR', product_type: 'VARCHAR', tags: 'VARCHAR[]', image: 'VARCHAR',
        variants: 'STRUCT(variant_id VARCHAR, title VARCHAR, price_usd DOUBLE, grams INTEGER, available BOOLEAN)[]',
        body_hash: 'VARCHAR', first_seen: 'DATE'
    }
)
