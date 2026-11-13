-- Every city a mart names met the city floors (design section 5): city_min_population people in the pinned GeoNames
-- file, events on city_min_days distinct days in the window and city_min_week_events in the week; every city x facet
-- cell holds city_cell_min_events events. Recomputed from int_usage_events and the geo tables, not the marts' SQL.
-- Returns one row per named city or cell that doesn't.
{{ config(severity='error') }}
{%- set w = var('usage_window_days') %}
with ev as (
    select * from {{ ref('int_usage_events') }} where not bot
),

named as (
    select distinct week, country, region, city from {{ ref('mart_usage_locations_weekly') }}
    where city not like 'other%' and city <> 'unknown'
    union
    select distinct week, country, region, city from {{ ref('mart_usage_city_facets_weekly') }}
    where city not like 'other%' and city <> 'unknown'
),

measured as (
    select n.week, n.country, n.region, n.city,
        max(c.population) as population,
        count(distinct e.received_date) filter (
            where e.received_date between n.week + 6 - ({{ w }} - 1) and n.week + 6) as days,
        count(*) filter (where e.week = n.week) as week_events
    from named as n
    left join {{ ref('stg_geo_cities') }} as c
        on c.country = n.country and c.region = n.region and c.city = lower(n.city)
    left join ev as e on e.country = n.country and e.region = n.region and e.city = n.city
    group by all
)

select 'city' as rule, week, coalesce(population, 0) as population, days, week_events as events
from measured
where coalesce(population, 0) < {{ var('city_min_population') }}
   or days < {{ var('city_min_days') }}
   or week_events < {{ var('city_min_week_events') }}
union all
select 'cell', week, null, null, events
from {{ ref('mart_usage_city_facets_weekly') }}
where events < {{ var('city_cell_min_events') }}
