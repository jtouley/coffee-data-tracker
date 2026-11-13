-- One row per (week, country, region, city): where events came from, by the places int_usage_event_places allows
-- (a city under 50,000 people or below the floors counts as "other, <region>"). City is where the IP block is
-- routed, often the provider's nearest metro: read "Denver" as "Denver-area networks". Bots left out.
-- page_views is held back (null) when it or the place's other events are under city_cell_min_events, so the split
-- of a place's events is never a small cell either.
with counted as (
    select e.week, p.country, p.region, p.city,
        count(*) as events,
        count(*) filter (where e.event = 'page_view') as page_views
    from {{ ref('int_usage_events') }} as e
    inner join {{ ref('int_usage_event_places') }} as p on p.event_id = e.event_id
    where not e.bot
    group by all
)

select week, country, region, city, events,
    if(page_views >= {{ var('city_cell_min_events') }}
       and events - page_views >= {{ var('city_cell_min_events') }}, page_views, null) as page_views
from counted
