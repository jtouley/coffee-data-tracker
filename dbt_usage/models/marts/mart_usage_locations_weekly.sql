-- One row per (week, country, region, city): where events came from, by the places int_usage_event_places allows
-- (a city under 50,000 people or below the floors counts as "other, <region>"). City is where the IP block is
-- routed, often the provider's nearest metro: read "Denver" as "Denver-area networks". Bots left out.
--
-- page_views is held back (null) when it or the place's other events are under city_cell_min_events, so the split
-- of a place's events is never a small cell. The week's page views are published whole
-- (mart_usage_page_views_weekly), so one place held back alone would be that total less the places shown: when
-- exactly one place is held back in a week, a second is held back too (complementary suppression): a place that
-- isn't a named city ('other, <region>', 'other', 'unknown') when there is one, so the cities stay, else the place
-- shown with the fewest page views. Two or more held back give only their sum, which is at least city_cell_min_events
-- each way (the second place's page views and other events are both at least that), never one place's count.
with counted as (
    select e.week, p.country, p.region, p.city,
        count(*) as events,
        count(*) filter (where e.event = 'page_view') as page_views
    from {{ ref('int_usage_events') }} as e
    inner join {{ ref('int_usage_event_places') }} as p on p.event_id = e.event_id
    where not e.bot
    group by all
),

judged as (
    select *,
        page_views < {{ var('city_cell_min_events') }}
        or events - page_views < {{ var('city_cell_min_events') }} as small
    from counted
),

ranked as (
    select *,
        count(*) filter (where small) over (partition by week) as small_places,
        row_number() over (
            partition by week, small
            order by city not like 'other%' and city <> 'unknown', page_views, events, country, region, city
        ) as nth
    from judged
)

select week, country, region, city, events,
    if(small or (small_places = 1 and nth = 1), null, page_views) as page_views
from ranked
