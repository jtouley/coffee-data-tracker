-- One row per event: the place it may be reported under (design section 5). A country or region is shown only once
-- it has events on usage_min_days days in the window; a city only with city_min_population people (GeoNames), events
-- on city_min_days days in the window and city_min_week_events in the week. Everything else counts as
-- "other, <region>" (or "other" in a region that isn't shown). Counts are over events that aren't bots.
with events as (
    select event_id, week, received_date, country, region, city, city_population, bot
    from {{ ref('int_usage_events') }}
),

people as (
    select * from events where not bot
),

country_days as {{ days_in_window('people', ['country']) }},

region_days as {{ days_in_window('people', ['country', 'region']) }},

city_days as {{ days_in_window('people', ['country', 'region', 'city']) }},

city_week as (
    select week, country, region, city, count(*) as events
    from people
    group by all
),

judged as (
    select e.event_id, e.country, e.region, e.city,
        coalesce(cd.days, 0) >= {{ var('usage_min_days') }} as country_shown,
        coalesce(rd.days, 0) >= {{ var('usage_min_days') }} as region_shown,
        coalesce(e.city_population, 0) >= {{ var('city_min_population') }}
            and coalesce(ct.days, 0) >= {{ var('city_min_days') }}
            and coalesce(cw.events, 0) >= {{ var('city_min_week_events') }} as city_shown
    from events as e
    left join country_days as cd on cd.week = e.week and cd.country is not distinct from e.country
    left join region_days as rd
        on rd.week = e.week and rd.country is not distinct from e.country and rd.region is not distinct from e.region
    left join city_days as ct
        on ct.week = e.week and ct.country is not distinct from e.country
        and ct.region is not distinct from e.region and ct.city is not distinct from e.city
    left join city_week as cw
        on cw.week = e.week and cw.country is not distinct from e.country
        and cw.region is not distinct from e.region and cw.city is not distinct from e.city
)

select
    event_id,
    case when country is null then 'unknown' when country_shown then country else '(rare)' end as country,
    case
        when country is null then 'unknown'
        when not country_shown then '(rare)'
        when region is null then 'unknown'
        when region_shown then region
        else '(rare)'
    end as region,
    case
        when country is null then 'unknown'
        when not country_shown or region is null or not region_shown then 'other'
        when city is not null and city_shown then city
        else 'other, ' || region
    end as city
from judged
