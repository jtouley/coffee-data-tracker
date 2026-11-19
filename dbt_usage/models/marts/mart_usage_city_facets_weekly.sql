-- One row per (week, country, region, city, facet, facet_value): one facet at a time beside the city (design
-- section 5: surface, device, origin, process, in-stock, sort, source), every cell of at least city_cell_min_events
-- events. Never with search text, a roaster or coffee, rank or a campaign tag, and never two facets at once
-- (no_city_with_query_or_roaster). Bots left out.
--
-- Secondary suppression, so no hidden cell can be worked out by subtraction, within a place or across places (a
-- facet value's total over every place is published, or nearly, elsewhere: sources_weekly for source, page views and
-- clicks for surface, the filter combinations for the filters):
-- - '(other)' is the same values at every place: a value is named, for a week and facet, when at least as many
--   places have city_cell_min_events or more of it as have fewer; the rest are '(other)' everywhere.
-- - A place whose named or '(other)' cell is under the floor has the whole facet held back for that week.
-- - When exactly one place is held back for a week and facet, a second is held back too (complementary
--   suppression): a place that isn't a named city when there is one, so the cities stay, else the place shown with
--   the fewest events in that facet.
-- What is hidden is then whole places' facets, at least two of them, over every value: the published totals give
-- only sums over two or more places, never one place's cell.
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

named as (
    select week, facet, facet_value,
        count(*) filter (where events >= {{ var('city_cell_min_events') }})
        >= count(*) filter (where events < {{ var('city_cell_min_events') }}) as named
    from cells
    group by all
),

merged as (
    select c.week, c.country, c.region, c.city, c.facet,
        if(n.named, c.facet_value, '(other)') as facet_value,
        sum(c.events) as events
    from cells as c
    inner join named as n on n.week = c.week and n.facet = c.facet and n.facet_value = c.facet_value
    group by all
),

judged as (
    select week, country, region, city, facet, sum(events) as events,
        min(events) < {{ var('city_cell_min_events') }} as small
    from merged
    group by all
),

ranked as (
    select *,
        count(*) filter (where small) over (partition by week, facet) as small_places,
        row_number() over (
            partition by week, facet, small
            order by city not like 'other%' and city <> 'unknown', events, country, region, city
        ) as nth
    from judged
),

facets_kept as (
    select week, country, region, city, facet
    from ranked
    where not small and not (small_places = 1 and nth = 1)
)

select m.week, m.country, m.region, m.city, m.facet, m.facet_value, cast(m.events as bigint) as events
from merged as m
semi join facets_kept as k
    on k.week = m.week and k.country = m.country and k.region = m.region and k.city = m.city and k.facet = m.facet
