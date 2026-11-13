-- One row per (week, country, region, city, facet, facet_value): one facet at a time beside the city (design
-- section 5: surface, device, origin, process, in-stock, sort, source), every cell of at least city_cell_min_events
-- events. Cells under the floor are merged into one '(other)' cell; when that cell is itself under the floor, the
-- whole facet is left out for that city and week (secondary suppression), so no hidden cell can be worked out by
-- subtracting the cells shown from a total. Never with search text, a roaster or coffee, rank or a campaign tag, and
-- never two facets at once (no_city_with_query_or_roaster). Bots left out.
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
),

cells as (
    select week, place_country as country, place_region as region, place_city as city, facet, facet_value,
        count(*) as events
    from faceted
    group by all
),

merged as (
    select week, country, region, city, facet,
        if(events < {{ var('city_cell_min_events') }}, '(other)', facet_value) as facet_value,
        sum(events) as events
    from cells
    group by all
),

facets_kept as (
    select week, country, region, city, facet
    from merged
    group by all
    having min(events) >= {{ var('city_cell_min_events') }}
)

select m.week, m.country, m.region, m.city, m.facet, m.facet_value, cast(m.events as bigint) as events
from merged as m
semi join facets_kept as k
    on k.week = m.week and k.country = m.country and k.region = m.region and k.city = m.city and k.facet = m.facet
