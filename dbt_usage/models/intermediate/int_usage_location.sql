-- One row per event: where its IP prefix is routed, from the pinned DB-IP file (config/geo.lock), and that city's
-- population from the pinned GeoNames file. The only model that reads ip_prefix, and it doesn't pass it on
-- (design section 4). The prefix's network address is looked up (the collector zeroed the host bits): the range
-- holding it, if any. A city with no population row is under 50,000 people or unknown, and is never named.
with prefixes as (
    select event_id, nullif(split_part(trim(ip_prefix), '/', 1), '') as addr
    from {{ ref('stg_usage_ip_prefixes') }}
),

keyed as (
    select event_id, {{ ip_to_hex('addr') }} as ip_hex from prefixes
),

found as (
    -- the last range starting at or before the address; it holds the address only if it ends at or after it
    select k.event_id,
        case when k.ip_hex <= r.ip_end then r.country end as country,
        case when k.ip_hex <= r.ip_end then r.region end as region,
        case when k.ip_hex <= r.ip_end then r.city end as city
    from keyed as k
    asof left join {{ ref('stg_geo_ranges') }} as r
        on k.ip_hex >= r.ip_start
)

select f.event_id, f.country, f.region, f.city, c.population as city_population
from found as f
left join {{ ref('stg_geo_cities') }} as c
    on c.country = f.country and c.region = f.region and c.city = lower(f.city)
