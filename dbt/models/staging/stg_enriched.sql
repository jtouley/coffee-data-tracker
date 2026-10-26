-- data/enriched/<roaster>.json is {product_id: {...}}; unpivot it to one row per product.
with raw as (
    select regexp_extract(filename, '([^/]+)\.json$', 1) as roaster_slug, json
    from read_json_objects('{{ var("data_dir") }}/enriched/*.json', filename = true)
),
kv as (select roaster_slug, json, unnest(json_keys(json)) as product_id from raw),
e as (select roaster_slug, product_id, json_extract(json, '$."' || product_id || '"') as j from kv)
select
    roaster_slug, product_id,
    j ->> 'origin_country' as origin_country, j ->> 'origin_region' as origin_region,
    j ->> 'producer' as producer, j ->> 'process' as process,
    cast(j -> 'varietals' as varchar[]) as varietals,
    try_cast(j ->> 'elevation_masl_min' as integer) as elevation_masl_min,
    try_cast(j ->> 'elevation_masl_max' as integer) as elevation_masl_max,
    j ->> 'roast_level' as roast_level,
    cast(j -> 'tasting_notes' as varchar[]) as tasting_notes,
    try_cast(j ->> 'is_blend' as boolean) as is_blend, try_cast(j ->> 'is_decaf' as boolean) as is_decaf,
    j ->> 'method' as enrichment_method
from e
