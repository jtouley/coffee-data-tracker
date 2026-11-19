-- One row per event: the place its IP prefix's whole block resolves to in the pinned DB-IP file (config/geo.lock),
-- and that city's population from the pinned GeoNames file. The only model that reads ip_prefix, and it doesn't pass
-- it on (design section 4).
--
-- Consensus, never a guess: the collector keeps only the block (IPv4 /24, IPv6 /40), and a block can hold rows
-- naming several places. Every row inside the block is read; the block gets the city only if they all name one
-- city, else the region if they all name one region, else the country if they all name one country, else nothing.
-- DB-IP's "City (Neighbourhood)" names are cut to the city's name first, so a block split by neighbourhood keeps its
-- city and the neighbourhood is never shown. Deterministic: a function of the locked file and the prefix.
-- DB-IP's filler rows count as unknown (see ranges). A city with no population row is under 50,000 people or
-- unknown, and is never named.
with prefixes as (
    select distinct ip_prefix,
        nullif(split_part(trim(ip_prefix), '/', 1), '') as addr,
        try_cast(nullif(split_part(trim(ip_prefix), '/', 2), '') as integer) as prefix_len
    from {{ ref('stg_usage_ip_prefixes') }}
    where ip_prefix is not null
),

blocks as (
    -- the block's first and last address as 32 hex digits. Its length counts in the 128-bit space (IPv4 is
    -- IPv4-mapped: /24 is /120), rounded down to a whole hex digit, so a length that isn't one widens the block
    -- (more rows, never fewer).
    select ip_prefix, start_hex,
        left(start_hex, keep) || repeat('f', 32 - keep) as end_hex
    from (
        select ip_prefix, start_hex,
            least(32, greatest(0, (coalesce(prefix_len, 0) + if(contains(addr, '.'), 96, 0)) // 4)) as keep
        from (select ip_prefix, addr, prefix_len, {{ ip_to_hex('addr') }} as start_hex from prefixes) as p
        where start_hex is not null and prefix_len is not null
    ) as k
),

ranges as (
    -- DB-IP's filler rows name no network's place: they are unknown, like its rows with no country
    -- (geo.is_filler: an IPv6 row reaching across 16 or more /32s, the size of a registry's own allocation, or
    -- placed at "CH, Fribourg, Murten/Morat", which DB-IP gives unannounced IPv6 space of any size)
    select ip_start, ip_end,
        if(filler, null, country) as country, if(filler, null, region) as region,
        if(filler, null, nullif(regexp_replace(city, '\s*\(.*\)\s*$', ''), '')) as city
    from (
        select *,
            left(ip_start, 24) <> '00000000000000000000ffff'
            and (
                try_cast('0x' || left(ip_end, 8) as ubigint) - try_cast('0x' || left(ip_start, 8) as ubigint) >= 15
                or (country = 'CH' and region = 'Fribourg' and city = 'Murten/Morat')
            ) as filler
        from {{ ref('stg_geo_ranges') }}
    ) as r
),

in_block as (
    -- the row holding the block's first address, if any ...
    select b.ip_prefix, r.country, r.region, r.city
    from blocks as b
    asof inner join ranges as r on b.start_hex >= r.ip_start
    where b.start_hex <= r.ip_end
    union all
    -- ... and every row starting inside the block after it
    select b.ip_prefix, r.country, r.region, r.city
    from blocks as b
    inner join ranges as r on r.ip_start > b.start_hex and r.ip_start <= b.end_hex
),

consensus as (
    select ip_prefix,
        count(distinct coalesce(country, '')) = 1 and bool_and(country is not null) as one_country,
        count(distinct coalesce(region, '')) = 1 and bool_and(region is not null) as one_region,
        count(distinct coalesce(city, '')) = 1 and bool_and(city is not null) as one_city,
        min(country) as country, min(region) as region, min(city) as city
    from in_block
    group by ip_prefix
),

placed as (
    select ip_prefix,
        if(one_country, country, null) as country,
        if(one_country and one_region, region, null) as region,
        if(one_country and one_region and one_city, city, null) as city
    from consensus
)

select e.event_id, p.country, p.region, p.city, c.population as city_population
from {{ ref('stg_usage_ip_prefixes') }} as e
left join placed as p on p.ip_prefix = e.ip_prefix
left join {{ ref('stg_geo_cities') }} as c
    on c.country = p.country and c.region = p.region and c.city = lower(p.city)
