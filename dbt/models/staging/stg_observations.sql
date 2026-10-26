select
    cast(observed_at as timestamptz) as observed_at, roaster_slug, product_id, variant_id, event, price_usd,
    available
from read_json(
    '{{ var("data_dir") }}/observations/**/*.jsonl',
    format = 'newline_delimited',
    columns = {
        observed_at: 'VARCHAR', roaster_slug: 'VARCHAR', product_id: 'VARCHAR', variant_id: 'VARCHAR',
        event: 'VARCHAR', price_usd: 'DOUBLE', available: 'BOOLEAN'
    }
)
