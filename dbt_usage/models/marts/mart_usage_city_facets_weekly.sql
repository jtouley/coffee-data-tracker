-- One row per (week, country, region, city, facet, facet_value): one facet at a time beside the city (design
-- section 5: surface, device, origin, process, in-stock, sort, source), each cell of at least city_cell_min_events
-- events; smaller cells are left out. Never with search text, a roaster or coffee, rank or a campaign tag, and never
-- two facets at once (no_city_with_query_or_roaster). Bots left out.
with events as (
    select e.*, p.country as place_country, p.region as place_region, p.city as place_city
    from {{ ref('int_usage_events') }} as e
    inner join {{ ref('int_usage_event_places') }} as p on p.event_id = e.event_id
    where not e.bot
),

landed as (
    select week, received_date, source_kind, source from events where source_kind is not null
),

source_days as {{ days_in_window('landed', ['source_kind', 'source']) }},

faceted as (
    select week, place_country, place_region, place_city, 'surface' as facet, coalesce(surface, 'unknown') as facet_value
    from events
    union all
    select week, place_country, place_region, place_city, 'device', coalesce(device, 'unknown') from events
    union all
    select week, place_country, place_region, place_city, 'origin', coalesce(origin, 'any')
    from events where event in ('search', 'filter_state')
    union all
    select week, place_country, place_region, place_city, 'process', coalesce(process, 'any')
    from events where event in ('search', 'filter_state')
    union all
    select week, place_country, place_region, place_city, 'in_stock', if(in_stock, 'yes', 'any')
    from events where event in ('search', 'filter_state')
    union all
    select week, place_country, place_region, place_city, 'sort', coalesce(sort, 'default')
    from events where event in ('search', 'filter_state')
    union all
    select e.week, e.place_country, e.place_region, e.place_city, 'source',
        case when s.days >= {{ var('usage_min_days') }} then e.source else '(rare)' end
    from events as e
    inner join source_days as s
        on s.week = e.week and s.source_kind = e.source_kind and s.source is not distinct from e.source
)

select week, place_country as country, place_region as region, place_city as city, facet, facet_value,
    count(*) as events
from faceted
group by all
having count(*) >= {{ var('city_cell_min_events') }}
